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

    # ── 嵌入 ──
    embed_backend: str = "fastembed"
    embed_model: str = "BAAI/bge-small-zh-v1.5"

    # ── 运行时 ──
    data_dir: Path = field(default_factory=lambda: Path("data"))

    # ── Bangumi（P1，§9.2）──
    # 只用来读**自己的**收藏（Task 28）。搜索与条目详情免 token，
    # 所以空着也能跑，doctor 只告警、validate() 不报错。
    bangumi_token: str = ""

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
    gate_timeout: float = 8.0

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
