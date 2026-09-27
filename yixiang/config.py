"""配置层：Settings dataclass + .env 解析 + 校验（TECH-DESIGN §3）。

设计原则：
  1. 全项目只有这里读 ``os.environ``；其他模块只读 ``Settings`` 对象。
  2. 字段名与 env 名一一对应：``YIXIANG_MAIN_MODEL`` → ``settings.main_model``。
  3. 优先级：命令行参数 > 环境变量 > ``.env`` > 代码默认值。
  4. **安全默认值要保守**：`YIXIANG_QQ_ALLOWED` 为空 = 拒绝一切外部消息。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

ENV_PREFIX = "YIXIANG_"

# 角色名（§4.2）：main / gate / utility / judge / embed
ROLES = ("main", "gate", "utility", "judge", "embed")

# 已知不支持 function calling 的模型名：**main 角色不能用它们**。
# main 是唯一必须能调工具的角色（查记忆、写备忘、读文件都靠它），配错了整条 agent loop
# 会静默退化成"只会聊天的聊天机器人"，而且不报错——所以要在启动前拦住。
# 其他角色（gate / judge / utility）不调工具，用便宜的 chat 档没问题，不在这条禁令里。
# 这一条只判"能力"；"名字还在不在售"由下面的 RETIRED_MODELS 管（两件事，两档）。
NO_TOOL_MODELS = ("deepseek-reasoner",)

# 已从官网下架的模型名：任何角色都不该再用它们。
# 证据（2026-09-20 实测）：``/v1/models`` 只返回 deepseek-flash / deepseek-v4-pro；
# 用下架的名字调用仍返回 200，但响应里的 model 是 deepseek-flash——即"还能用"只是一个
# 兼容别名在兜底，别名一撤，每次调用都会变成 400。
RETIRED_MODELS = ("deepseek-chat", "deepseek-reasoner")

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off", ""}


def parse_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析：``KEY=VALUE``、``#`` 注释、可选引号。不引第三方库。"""
    if not path.is_file():
        return {}
    return parse_env_text(path.read_text(encoding="utf-8"))


def parse_env_text(text: str) -> dict[str, str]:
    """解析 .env 的**文本**（``parse_env_file`` 与 Web 控制台的试算共用一套规则）。"""
    values: dict[str, str] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        # 去掉行尾注释（只在未被引号包裹时处理，避免吃掉值里的 #）
        if value[:1] not in {'"', "'"}:
            value = value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _as_bool(raw: str, *, name: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(f"{name} 需要布尔值（1/0/true/false），收到 {raw!r}")


@dataclass(slots=True)
class Settings:
    """运行期配置。字段顺序 = .env.example 的顺序，便于对照维护。"""

    # ── 模型（角色路由）──
    main_model: str = "deepseek-flash"
    gate_model: str = ""
    judge_model: str = ""
    utility_model: str = ""
    api_base: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    # judge / utility 可以换一家（Task 9）：留空 = 跟 main 同一家，老行为一个字节不变。
    # 本机 Ollama（``http://127.0.0.1:11434/v1``）没有密钥，所以 judge_api_key 允许为空——
    # 为空时**连 Authorization 头都不发**（本机端点被多余的 Bearer 反而可能拒掉）。
    judge_api_base: str = ""
    judge_api_key: str = ""
    # 关掉"思考模式"的模型名单（逗号分隔，支持 `qwen3.5-*` 这样的后缀通配）。
    # 为什么需要它：会思考的模型先把思考过程写进 ``reasoning``，**而它和正文共用同一个
    # ``max_tokens`` 预算**——不关的话上限会被思考吃光、``content`` 为空。在 OpenAI
    # 兼容端点上**唯一生效**的开关是请求体里的 ``reasoning_effort="none"``（实测：
    # ``chat_template_kwargs={"enable_thinking": false}`` 与 ``think=false`` 都不透传；
    # ``thinking={"type":"disabled"}`` 只是不返回 reasoning_content，token 照烧）。
    # 本机建议把云端那家也写进去：``deepseek-*``（deepseek-flash 实测会把 8192 个
    # token 全花在思考上，正文一个字没吐）。
    # 留空 = 谁都不关（老行为）。
    no_think_models: str = ""

    # ── 嵌入 ──
    embed_backend: str = "fastembed"
    embed_model: str = "BAAI/bge-small-zh-v1.5"

    # ── 运行时 ──
    data_dir: Path = field(default_factory=lambda: Path("data"))

    # ── Bangumi（P1，§9.2）──
    # 只用来读**自己的**收藏（Task 28）。搜索与条目详情免 token，
    # 所以空着也能跑，doctor 只告警、validate() 不报错。
    bangumi_token: str = ""
    # 只给 Bangumi 出口用的代理。为什么要这一项：本机直连 ``api.bgm.tv:443`` 是
    # **超时**（8.3s ConnectTimeout，开着 VPN 也一样——VPN 是代理模式、不接管直连），
    # 而走 ``127.0.0.1:7897`` 上那个代理时全部接口 200。写全局 ``HTTPS_PROXY`` 会让
    # **所有**出站流量改道（模型端点、TMDb、将来任何一个新接口），粒度太粗，所以收成
    # 这一项：只喂给 Bangumi 的四条链路（实时搜索 / 条目详情 / 收藏画像 / 批处理抓取）。
    # 留空 = 老行为一个字节不变（httpx 照旧看环境变量与系统代理）；validate() 不为它报错。
    bangumi_proxy: str = ""

    # ── QQ（P2）──
    qq_enabled: bool = False
    # 8766 而不是 8765：8765 是 Web 控制台（``web.server.DEFAULT_PORT``）的默认端口。
    # 两个监听撞一起时，报错指向的是"端口被占用"而不是"你配重了"——排查成本极高
    # （表现是"Web 起不来"或"QQ 收不到消息"，跟端口无关的报错）。
    qq_listen: str = "127.0.0.1:8766"
    qq_token: str = ""
    qq_allowed: str = ""
    qq_group_enabled: bool = False

    # ── 调度 ──
    scheduler_enabled: bool = False
    brief_cron: str = "0 8 * * *"
    brief_catchup_until: str = "12:00"
    brief_sink: str = "cli,file"

    # ── 记忆与上下文预算 ──
    consolidate_every: int = 20
    history_turns: int = 10
    retrieve_top_k: int = 5
    episode_top_k: int = 3

    # ── Loop 与超时 ──
    loop_max_iter: int = 8
    tool_retry_max: int = 2
    llm_timeout: float = 60.0
    # 单次回复的输出上限（token）。2048 那个默认值实测撑不住正常需求：一句"鉴赏一下
    # 这张封面"就能把 2048 写满。改成 8192 之后**还是**被截断，才挖到真正的病根：
    # 会思考的模型把 ``reasoning`` 也算进这同一个预算（2026-09-27 直连探针：10000
    # token 全花在思考上，正文一个字没吐）。所以它是"正文 + 思考"的**总额**，不是
    # 文档里那个"最大输出长度"；真正写多长由模型自己决定，它不会为了凑满而硬写。
    # loop 侧另有一道保险：撞线先自动关掉思考重问一次（§5.1 的 length 分支）。
    max_tokens: int = 8192
    gate_timeout: float = 8.0
    # 嵌入后端最长等多久：fastembed 首载要下模型、jieba 首载要建词典，
    # 卡住时的表现是"整轮对话不动"，必须有个上限（超时即降级纯 FTS，D-24）
    embed_timeout: float = 20.0

    # ── 成本与日志 ──
    budget_cny_per_day: float = 0.5
    log_level: str = "INFO"

    # ── 非 env 字段（程序内部使用）──
    project_root: Path = field(default_factory=lambda: Path.cwd())
    env_file: Path | None = None

    # ------------------------------------------------------------------ 构造
    @classmethod
    def load(
        cls,
        *,
        env_file: Path | str | None = ".env",
        environ: Mapping[str, str] | None = None,
        project_root: Path | str | None = None,
        **overrides: Any,
    ) -> Settings:
        """按优先级装配 Settings：overrides > environ > .env > 默认值。"""
        env = dict(os.environ if environ is None else environ)
        root = Path(project_root) if project_root is not None else Path.cwd()
        env_path = Path(env_file) if env_file is not None else None
        if env_path is not None and not env_path.is_absolute():
            env_path = root / env_path
        file_values = parse_env_file(env_path) if env_path is not None else {}

        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name in {"project_root", "env_file"}:
                continue
            env_name = ENV_PREFIX + f.name.upper()
            raw = env.get(env_name, file_values.get(env_name))
            if raw is None or raw == "":
                continue
            kwargs[f.name] = _coerce(f.name, raw, f.type)
        kwargs.update({k: v for k, v in overrides.items() if v is not None})

        settings = cls(**kwargs)
        settings.project_root = root
        settings.env_file = env_path
        if not settings.data_dir.is_absolute():
            settings.data_dir = (root / settings.data_dir).resolve()
        return settings

    # ------------------------------------------------------------------ 校验
    def validate(self) -> list[str]:
        """启动时一次性校验；返回的错误会被 doctor 打印成"缺什么、去哪填"。"""
        errors: list[str] = []
        if not self.api_key:
            errors.append(
                "YIXIANG_API_KEY 缺失：复制 .env.example 为 .env 并填入密钥"
            )
        if self.qq_enabled and not self.qq_allowed.strip():
            errors.append(
                "YIXIANG_QQ_ENABLED=1 但 YIXIANG_QQ_ALLOWED 为空："
                "出于安全考虑拒绝启动 QQ 网关"
            )
        if not 1 <= self.loop_max_iter <= 20:
            errors.append("YIXIANG_LOOP_MAX_ITER 应在 1~20")
        # 下限 256：再小连一句完整的话都放不下，撞线必然发生，等于把配置写成了故障。
        if not 256 <= self.max_tokens <= 65536:
            errors.append("YIXIANG_MAX_TOKENS 应在 256~65536")
        if not 1 <= self.embed_timeout <= 120:
            errors.append("YIXIANG_EMBED_TIMEOUT 应在 1~120")
        if self.main_model in NO_TOOL_MODELS:
            errors.append(
                f"YIXIANG_MAIN_MODEL={self.main_model} 不支持工具调用："
                "主模型必须能调工具（查记忆 / 写备忘都靠它），换成 deepseek-flash "
                "或别的支持 function calling 的模型；窄角色（gate / judge / utility）"
                "不调工具，可以继续用它"
            )
        for role, model in (
            ("MAIN", self.main_model),
            ("GATE", self.gate_model),
            ("JUDGE", self.judge_model),
            ("UTILITY", self.utility_model),
        ):
            if model in RETIRED_MODELS:
                errors.append(
                    f"YIXIANG_{role}_MODEL={model} 已下架：官网在售只有 deepseek-flash / "
                    "deepseek-v4-pro。这个名字现在会被服务端静默换成 deepseek-flash，"
                    "别名一撤就是每次调用报错"
                )
        if self.history_turns < 0:
            errors.append("YIXIANG_HISTORY_TURNS 不能为负")
        if self.tool_retry_max < 0:
            errors.append("YIXIANG_TOOL_RETRY_MAX 不能为负")
        # hash 不是"玩具"：它是**离线等价后端**（`rag/embed.py` 的 HashEmbedder），
        # CI 与无网演示都靠它把检索链路跑成确定性的，所以白名单必须有它。
        if self.embed_backend not in {"fastembed", "sentence-transformers", "api", "hash"}:
            errors.append(
                "YIXIANG_EMBED_BACKEND 只支持 fastembed / sentence-transformers / api / hash"
            )
        return errors

    # -------------------------------------------------------- 换家与思考模式
    @property
    def judge_base(self) -> str:
        """judge / utility 角色的端点根：没配就用 main 那家（向后兼容）。"""
        return (self.judge_api_base or self.api_base).strip()

    @property
    def judge_auth_key(self) -> str:
        """judge 家要用的密钥。
        **换了家却没给密钥 = 就是不给密钥**（本机 Ollama 正是这样）：这里绝不能回落到
        main 的 key——那等于把 DeepSeek 的密钥原样发给另一个端点。只有"没换家"时才回落。
        """
        if self.judge_api_base.strip():
            return self.judge_api_key
        return self.judge_api_key or self.api_key
    @property
    def no_think_patterns(self) -> tuple[str, ...]:
        """``YIXIANG_NO_THINK_MODELS`` 解析成的名单：去空白、丢空项。"""
        return tuple(
            item for item in (part.strip() for part in self.no_think_models.split(",")) if item
        )

    def thinking_disabled_for(self, model: str) -> bool:
        """这个模型要不要在请求里带上 ``reasoning_effort="none"``（按**模型名**判，不按角色）。
        名单项 = 精确模型名，或 ``前缀*``（本地模型名几乎都带 ``:latest`` 之类的标签，
        每次 ollama pull 都可能变，写前缀比逐个写全名稳）。
        """
        name = (model or "").strip()
        if not name:
            return False
        for pattern in self.no_think_patterns:
            if pattern.endswith("*"):
                if name.startswith(pattern[:-1]):
                    return True
            elif name == pattern:
                return True
        return False

    # ------------------------------------------------------------------ 路由
    def model_for(self, role: str) -> str:
        """角色路由（§4.2）：窄决策角色留空时回落到上游角色。"""
        match role:
            case "main":
                return self.main_model
            case "gate":
                return self.gate_model or self.main_model
            case "judge":
                return self.judge_model or self.main_model
            case "utility":
                return self.utility_model or self.judge_model or self.main_model
            case "embed":
                return self.embed_model
            case _:
                raise ValueError(f"未知角色 {role!r}，允许：{ROLES}")

    # ------------------------------------------------------------------ 路径
    @property
    def traces_dir(self) -> Path:
        return self.data_dir / "traces"

    @property
    def usage_path(self) -> Path:
        return self.data_dir / "usage.jsonl"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def templates_dir(self) -> Path:
        return self.project_root / "templates"

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"

    def describe(self) -> str:
        """一行为主的人话摘要（doctor / serve 打印用）。"""
        key_state = "已配置" if self.api_key else "未配置"
        return (
            f"data_dir={self.data_dir} · main={self.main_model} · "
            f"gate={self.model_for('gate')} · api_key={key_state}"
        )


def _coerce(name: str, raw: str, type_hint: Any) -> Any:
    """把 env 字符串转成字段类型。bool 必须显式解析，不能靠 ``bool("0")``。"""
    hint = str(type_hint)
    if "bool" in hint:
        return _as_bool(raw, name=name)
    if "Path" in hint:
        return Path(raw)
    if "int" in hint:
        return int(raw)
    if "float" in hint:
        return float(raw)
    return raw
