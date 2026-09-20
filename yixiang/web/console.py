"""Web 控制台的业务适配层（TECH §10 的第三个入口，ADR-3）。

这一层只做"翻译"：把请求翻译成对 ``App`` 与 ``data/`` 的调用，本身不认识 HTTP、
不写 JSON、不管 SSE 的字节格式（那些在 ``server.py``）。所以 Web 端要验证的东西——
人设有没有改、记忆有没有超限、QQ 白名单拦不拦得住——都能在**不启服务**的情况下用
用例断言（``evals/deterministic/test_web.py``）。

三条纪律，与项目其它部分保持同一条：

  1. **入口只搬文本**：对话链路只有 ``App.handle_message()`` 一条，这里不复制 loop；
  2. **写文件走既有出口**：人设 / 记忆用 ``core_files.write_core_file()``（原子写 +
     上限校验），配置用 ``config.parse_env_text`` 解析后原子写回 ``.env``——
     不新写第二套解析器，避免"网页里存得进去、命令行读不出来"；
  3. **密钥只出掩码**：``GET`` 系列永远不回明文 ``api_key`` / ``qq_token``（§14.2）。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable, Iterable
from dataclasses import fields
from pathlib import Path
from typing import Any

from yixiang.app import App
from yixiang.config import ENV_PREFIX, Settings, parse_env_text
from yixiang.memory import core_files, procedural, sync
from yixiang.memory.core_files import CHAR_LIMITS, MEMORY_MAX_LINES, LimitExceeded
from yixiang.ops.usage import summarize
from yixiang.runtime.models import LoopEvent
from yixiang.runtime.session import CONTEXT_BUDGET_CHARS, read_core_file

# 上传件的落点（data/ 之内，工具 read_file 的沙箱里）
UPLOADS_SUBDIR = "uploads"
# 单个上传件上限：本地测试够用，又不会让一个 2GB 的文件把内存打爆
MAX_UPLOAD_BYTES = 2_000_000
# 会话 id 长度上限（它会被写进 chat_log 与 trace，不该无限长）
SESSION_ID_LIMIT = 120
# Web 控制台自己的默认会话：与 CLI 的 cli:default 分开，source 才不会漂
DEFAULT_SESSION_ID = "web:default"

# 可编辑的人设文件（memory.md 走 /api/memory——它的口径是"行"而不是"字"）
PERSONA_FILES = ("soul.md", "user.md")
PERSONA_LABELS = {"soul.md": "人设（soul.md）", "user.md": "用户画像（user.md）"}

# ── 可写回 .env 的字段白名单（不在表里的一律忽略，绝不用 payload 造键）──
CONFIG_FIELDS = (
    "main_model",
    "gate_model",
    "judge_model",
    "utility_model",
    "api_base",
    "api_key",
    "embed_backend",
    "embed_model",
    "data_dir",
    "consolidate_every",
    "history_turns",
    "retrieve_top_k",
    "episode_top_k",
    "loop_max_iter",
    "tool_retry_max",
    "llm_timeout",
    "gate_timeout",
    "budget_cny_per_day",
    "log_level",
    "scheduler_enabled",
    "brief_cron",
    "brief_catchup_until",
    "brief_sink",
)
QQ_FIELDS = ("qq_enabled", "qq_listen", "qq_token", "qq_allowed", "qq_group_enabled")
# 密钥类字段：空串 = "这次不改"（前端拿到的是掩码，回填空串才不会被写成空值）
SECRET_FIELDS = ("api_key", "qq_token")

# 下拉候选。模型名**不是白名单**（vLLM / Ollama 可以填任意名字），只作为建议值
MODEL_SUGGESTIONS = ("deepseek-chat", "deepseek-reasoner")
EMBED_CHOICES = ("fastembed", "sentence-transformers", "api", "hash")
LOG_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR")

# S1~S8 的段名（§6.1）——前端"提示词"面板按这个顺序展示
BLOCK_LABELS = (
    ("S1", "身份与行为守则", "data/soul.md（Learned rules 之前）"),
    ("S2", "Learned rules", "data/soul.md（## Learned rules 段）"),
    ("S3", "用户画像", "data/user.md"),
    ("S4", "长期记忆核心区", "data/memory.md（不含待确认 / 手写笔记 / 归档）"),
    ("S5", "相关技能", "data/skills/*/SKILL.md（按触发词匹配）"),
    ("S6", "本轮检索记忆", "gate 命中后检索 facts / episodes"),
    ("S7", "环境时间", "代码生成（精确到分钟）"),
    ("S8", "本轮契约", "代码生成（REMEMBER 打标 / 纠错重试）"),
)

Emit = Callable[[dict[str, Any]], None]


class ConsoleError(Exception):
    """给前端看的错误：带 HTTP 状态码与结构化 payload（错误要可行动）。"""

    def __init__(
        self,
        message: str,
        *,
        status: int = 400,
        code: str = "bad_request",
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.payload: dict[str, Any] = dict(payload or {})


class ConsoleAPI:
    """控制台的唯一业务入口。一个进程一个实例（内部持有唯一的 App 与连接）。"""

    def __init__(
        self,
        settings: Settings,
        *,
        app: App | None = None,
        app_factory: Callable[[Settings], App] | None = None,
        env_file: Path | str | None = None,
    ) -> None:
        self.settings = settings
        self.env_file: Path | None = (
            Path(env_file) if env_file is not None else settings.env_file
        )
        self._app = app
        # 想换装配方式（用例注入假 Provider）就换这个工厂，而不是在外面先建好 App：
        # App 里的 sqlite 连接归**建它的那条线程**所有，谁建谁用。
        self.app_factory = app_factory or (lambda current: App.from_settings(current))
        self._entered = False

    # ------------------------------------------------------------------ 装配
    @property
    def app(self) -> App:
        """懒装配：谁先用到它就在哪个线程建。

        ``sqlite3`` 的连接不能跨线程用（``db.py`` 的纪律），而 HTTP 层是"哪个线程
        处理请求就在哪个线程跑"；把 App 的构造推迟到第一次访问，两条纪律就都对上了
        （``serve()`` 会在同一条线程里显式预热一次，启动错误照样能立刻看到）。
        """
        if self._app is None:
            self._app = self.app_factory(self.settings)
            self._entered = False
        if not self._entered:
            # 默认落在 web:default：source 记"这句话从哪个入口进来"，与 CLI 分得开
            self._app.switch_session(
                DEFAULT_SESSION_ID,
                source=self._app.session.source_of(DEFAULT_SESSION_ID, "web"),
            )
            self._entered = True
        return self._app

    # ------------------------------------------------------------------ 总览
    def state(self) -> dict[str, Any]:
        """一屏看全：模型、密钥状态、人设用量、QQ、工具与技能数、今日成本。"""
        settings = self.settings
        session = self.app.session
        registry = self.app.registry
        return {
            "app": {
                "data_dir": str(settings.data_dir),
                "env_file": str(self.env_file or ""),
                "project_root": str(settings.project_root),
            },
            "models": {
                role: settings.model_for(role)
                for role in ("main", "gate", "judge", "utility")
            },
            "provider": {
                "api_base": settings.api_base,
                "api_key_set": bool(settings.api_key),
                "api_key_mask": mask_secret(settings.api_key),
                "embed_backend": settings.embed_backend,
                "embed_model": settings.embed_model,
            },
            "session": {
                "id": session.session_id,
                "source": session.source,
                "turns": len(session.transcript()),
            },
            "memory": self._usage_summary(),
            "qq": self.qq(),
            "runtime": {
                "history_turns": settings.history_turns,
                "context_budget_chars": CONTEXT_BUDGET_CHARS,
                "loop_max_iter": settings.loop_max_iter,
                "tool_retry_max": settings.tool_retry_max,
                "llm_timeout": settings.llm_timeout,
                "budget_cny_per_day": settings.budget_cny_per_day,
                "scheduler_enabled": settings.scheduler_enabled,
            },
            "counters": {
                "tools": len(registry) if registry else 0,
                "skills": self._skills()["count"],
            },
            "ready": {
                "api_key": bool(settings.api_key),
                "memory": self.app.memory_ready,
                "rag": self.app.rag_ready,
            },
            "cost": self.cost(),
            # 启动自检的红灯照实回给前端（缺 key / QQ 开了没白名单…）
            "errors": settings.validate(),
        }

    def cost(self, *, period: str = "day") -> dict[str, Any]:
        """今日（或本月）用量：口径与 ``yixiang ops cost`` 完全同一个函数。"""
        summary = summarize(
            self.settings.usage_path, period=period, ref=self.app.clock.now().date()
        )
        return {"period": period, "ref": summary["ref"], "total": summary["total"]}

    # ------------------------------------------------------------------ 会话
    def sessions(self, limit: int = 30) -> dict[str, Any]:
        session = self.app.session
        return {
            "current": session.session_id,
            "sessions": session.list_sessions(limit=max(int(limit), 1)),
        }

    def switch(self, session_id: str) -> dict[str, Any]:
        """切到某个历史会话（沿用它原来的 source，别把来源改成 web）。"""
        target = _clean_session_id(session_id)
        self.app.switch_session(target, source=self.app.session.source_of(target, "web"))
        return self.sessions() | {"current": target}

    def new_session(self, name: str | None = None) -> dict[str, Any]:
        """开一个新会话：``web:20260920-1530[-名字]``（与 CLI ``/new`` 同规则）。"""
        self.app.session.new_session(name)
        return self.sessions()

    def transcript(self, session_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        """某个会话的完整往来（点历史列表里的某一条就看这个，顺带切过去）。"""
        switched = False
        if session_id and session_id != self.app.session.session_id:
            self.switch(session_id)
            switched = True
        session = self.app.session
        return {
            "session_id": session.session_id,
            "source": session.source,
            "switched": switched,
            "turns": session.transcript(limit=max(int(limit), 1)),
        }

    # ------------------------------------------------------------------ 人设
    def persona(self) -> dict[str, Any]:
        """人设两文件：正文 + 用量（读实体，实体不存在时回落模板并如实标注）。"""
        return {"files": [self._persona_file(name) for name in PERSONA_FILES]}

    def save_persona(self, name: str, text: str) -> dict[str, Any]:
        """原子写回人设（超限一个字节都不动，错误里带上限与实际值）。"""
        if name not in PERSONA_FILES:
            raise ConsoleError(
                f"只能改 {' / '.join(PERSONA_FILES)}，收到 {name!r}",
                code="bad_field",
                payload={"field": name, "allowed": list(PERSONA_FILES)},
            )
        self._write_core(name, text)
        return {"ok": True, **self.persona()}

    # ------------------------------------------------------------------ 记忆
    def memory(self) -> dict[str, Any]:
        """memory.md 全文 + 活跃行数 + 解析出的条目（网页上要能看清每条 id）。"""
        path = core_files.ensure_memory_file(
            self.settings.data_dir, template_dir=self.settings.templates_dir
        )
        text = path.read_text(encoding="utf-8")
        doc = core_files.parse_memory_md(text)
        return {
            "path": str(path),
            "text": text,
            "used_lines": core_files.active_line_count(text),
            "max_lines": MEMORY_MAX_LINES,
            "sections": doc.sections,
            "entries": [
                {
                    "id": entry.fact_id,
                    "line": entry.index + 1,
                    "section": entry.section,
                    "content": entry.content,
                    "pinned": entry.pinned,
                }
                for entry in doc.entries
            ],
            "problems": core_files.validate_memory_md(text),
        }

    def save_memory(self, text: str) -> dict[str, Any]:
        self._write_core("memory.md", text)
        return {"ok": True, **self.memory()}

    def sync_memory(self) -> dict[str, Any]:
        """把 memory.md 同步进数据库（文件为准）——网页上改完文件按这个按钮。"""
        if self.app.conn is None:
            raise ConsoleError("数据库连接不可用（App 已关闭？）", status=500)
        report = sync.sync_memory_md(self.app.conn)
        return {"summary": report.summary(), "warnings": list(report.warnings)}

    # ------------------------------------------------------------------ 配置
    def config(self) -> dict[str, Any]:
        """可编辑配置的当前值（密钥明文永不出站，只给掩码）。"""
        settings = self.settings
        values: dict[str, Any] = {}
        for name in CONFIG_FIELDS:
            raw = getattr(settings, name)
            values[name] = str(raw) if isinstance(raw, Path) else raw
        values["api_key"] = ""  # 要改就填新的；留空 = 不改
        return {
            "fields": values,
            "secret_mask": {"api_key": mask_secret(settings.api_key)},
            "choices": {
                "model_suggestions": list(MODEL_SUGGESTIONS),
                "embed_backend": list(EMBED_CHOICES),
                "log_level": list(LOG_CHOICES),
            },
            "env_file": str(self.env_file or ""),
            "env_file_exists": bool(self.env_file and self.env_file.is_file()),
            "errors": settings.validate(),
        }

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """合并写回 ``.env`` 并立刻生效（只挡**新引入**的问题，旧问题不拦你改别的）。"""
        return self._save_env(payload, allowed=CONFIG_FIELDS, namespace="config")

    # ------------------------------------------------------------------ QQ
    def qq(self) -> dict[str, Any]:
        settings = self.settings
        allowed = [item.strip() for item in settings.qq_allowed.split(",") if item.strip()]
        return {
            "fields": {
                "qq_enabled": settings.qq_enabled,
                "qq_listen": settings.qq_listen,
                "qq_token": "",
                "qq_allowed": settings.qq_allowed,
                "qq_group_enabled": settings.qq_group_enabled,
            },
            "secret_mask": {"qq_token": mask_secret(settings.qq_token)},
            "allowed_list": allowed,
            "allowed_count": len(allowed),
            "status": _qq_status(settings.qq_enabled, allowed),
            "note": "P2 才接网关：这里的配置会写进 .env，QQ 入口落地后直接生效。",
        }

    def save_qq(self, payload: dict[str, Any]) -> dict[str, Any]:
        """QQ 设置白名单校验就在这里挡住：开了网关却没有白名单 → 直接拒绝。"""
        return self._save_env(payload, allowed=QQ_FIELDS, namespace="qq") | {
            "qq": self.qq()
        }

    # ------------------------------------------------------------------ 提示词
    def prompt(self) -> dict[str, Any]:
        """S1~S8 的**实况**（本轮现拼的结果）：让"模型到底看到了什么"可自查。"""
        blocks = self.app.session.system_blocks()
        used = len("\n\n".join(block for block in blocks if block))
        items = [
            {
                "id": block_id,
                "label": label,
                "source": source,
                "chars": len(text),
                "text": text,
            }
            for (block_id, label, source), text in zip(BLOCK_LABELS, blocks, strict=True)
        ]
        return {
            "blocks": items,
            "used_chars": used,
            "budget_chars": CONTEXT_BUDGET_CHARS,
            "note": "S5 / S6 只有在一轮对话进行中才有内容，这里是空闲状态。",
            "editable": "S1~S4 的正文在「人设与记忆」面板里改（同一份 data/*.md）。",
        }

    # ------------------------------------------------------------------ 工具/技能
    def tools(self) -> dict[str, Any]:
        registry = self.app.registry
        items = []
        for tool in registry.find("") if registry else []:
            items.append(
                {
                    "name": tool.name,
                    "side_effect": bool(tool.side_effect),
                    "args": list(tool.input_schema.get("properties") or {}),
                    "required": list(tool.input_schema.get("required") or []),
                    "description": tool.description,
                }
            )
        return {"count": len(items), "tools": items}

    def skills(self) -> dict[str, Any]:
        return self._skills()

    # ------------------------------------------------------------------ 上传
    def upload(self, filename: str, data: bytes) -> dict[str, Any]:
        """把上传件落到 ``data/uploads/``，返回相对路径（模型读它用 read_file）。"""
        if not data:
            raise ConsoleError("文件是空的：选一个非空文件再传。", code="empty_file")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ConsoleError(
                f"文件 {len(data)} 字节，超过上限 {MAX_UPLOAD_BYTES} 字节（约 2MB）；"
                "先截断，或转成更小的 .md / .txt 再传。",
                status=413,
                code="too_large",
                payload={"limit": MAX_UPLOAD_BYTES, "actual": len(data)},
            )
        directory = self.settings.data_dir / UPLOADS_SUBDIR
        directory.mkdir(parents=True, exist_ok=True)
        stamp = self.app.clock.now().strftime("%Y-%m-%d")
        target = _unique_path(directory, f"{stamp}-{_safe_upload_name(filename)}")
        target.write_bytes(data)
        relative = f"{UPLOADS_SUBDIR}/{target.name}"
        return {
            "path": relative,
            "name": target.name,
            "bytes": len(data),
            "dir": str(directory),
            "hint": f'让我读它：read_file(path="{relative}")',
        }

    # ------------------------------------------------------------------ 对话
    def chat(self, text: str, emit: Emit | None = None) -> dict[str, Any]:
        """跑一轮（流式）：事件实时喂给 ``emit``，返回值是这一轮的结果快照。"""
        message = (text or "").strip()
        if not message:
            raise ConsoleError("消息是空的：写一句再发送。", code="empty_message")
        if not self.settings.api_key:
            raise ConsoleError(
                "还没配 API key：去「模型配置」面板填 YIXIANG_API_KEY 再聊。",
                code="no_api_key",
            )

        def observer(event: LoopEvent) -> None:
            if emit is None:
                return
            try:
                emit({"kind": event.kind, **dict(event.data or {})})
            except Exception:  # noqa: BLE001 - 浏览器断开不能把这一轮掐断（trace 要写完）
                return

        try:
            result = asyncio.run(
                self.app.handle_message(message, observer=observer, stream=True)
            )
        except Exception as exc:  # noqa: BLE001 - 如实回错，不让 500 页面吞掉原因
            raise ConsoleError(f"这一轮没能跑完：{exc}", status=500, code="turn_failed") from exc
        payload = {
            "turn_id": result.turn_id,
            "reply": result.reply,
            "model": result.model,
            "finish_reason": result.finish_reason,
            "error": result.error,
            "error_detail": result.error_detail,
            "iterations": result.iterations,
            "latency_ms": result.latency_ms,
            "usage": result.usage.as_dict(),
            "memory_write_failed": result.memory_write_failed,
            "tools": [event.as_trace() for event in result.tool_calls],
        }
        if emit is not None:
            with contextlib.suppress(Exception):  # 结果照常落盘，只是没送出去
                emit({"kind": "result", **payload})
        return payload

    # ------------------------------------------------------------------ 内部
    def close(self) -> None:
        if self._app is not None:
            self._app.close()
        self._app = None
        self._entered = False

    def _persona_file(self, name: str) -> dict[str, Any]:
        root = self.settings.data_dir
        text = read_core_file(self.settings, name)
        limit = CHAR_LIMITS[name]
        used = len(text.strip())
        return {
            "name": name,
            "label": PERSONA_LABELS[name],
            "text": text,
            "used": used,
            "limit": limit,
            "unit": "字符",
            "source": "data" if (root / name).is_file() else "template",
            "over": used > limit,
        }

    def _usage_summary(self) -> dict[str, Any]:
        """三文件用量：人设按字符、memory.md 按行（口径与各自的写入口一致）。"""
        persona = {item["name"]: item for item in self.persona()["files"]}
        memory = self.memory()
        return {
            "soul": {
                "used": persona["soul.md"]["used"],
                "limit": persona["soul.md"]["limit"],
            },
            "user": {
                "used": persona["user.md"]["used"],
                "limit": persona["user.md"]["limit"],
            },
            "memory": {"used": memory["used_lines"], "limit": memory["max_lines"]},
        }

    def _skills(self) -> dict[str, Any]:
        loader = procedural.SkillLoader([self.settings.skills_dir])
        return {
            "count": len(loader.skills),
            "skills": [
                {
                    "name": skill.name,
                    "slug": skill.slug,
                    "description": skill.description,
                    "triggers": list(skill.triggers),
                }
                for skill in loader.all()
            ],
            "warnings": list(loader.warnings),
        }

    def _write_core(self, name: str, text: str) -> None:
        try:
            core_files.write_core_file(self.settings.data_dir, name, text)
        except LimitExceeded as exc:
            raise ConsoleError(
                f"{exc.name} 超过上限：{exc.actual} {exc.unit} > {exc.limit} {exc.unit}。"
                "文件没有被改动——先删减再保存。",
                code="limit_exceeded",
                payload={
                    "field": exc.name,
                    "limit": exc.limit,
                    "actual": exc.actual,
                    "unit": exc.unit,
                },
            ) from exc

    def _save_env(
        self, payload: dict[str, Any], *, allowed: Iterable[str], namespace: str
    ) -> dict[str, Any]:
        """白名单合并 → 试算 settings → 原子写回 → 热更新运行中的 Settings。"""
        if self.env_file is None:
            raise ConsoleError(
                "没有可写的 .env 路径（启动时 --env-file 为空）；改用命令行手工配置。",
                status=500,
                code="no_env_file",
            )
        allowed_set = set(allowed)
        updates: dict[str, str] = {}
        ignored: list[str] = []
        for key, value in dict(payload or {}).items():
            if key not in allowed_set:
                ignored.append(str(key))
                continue
            if key in SECRET_FIELDS and str(value or "").strip() == "":
                continue  # 掩码回填的空串 = 这次不改
            updates[ENV_PREFIX + key.upper()] = _env_value(value)
        if not updates:
            raise ConsoleError(
                "没有需要保存的字段（密钥留空表示不修改）。",
                code="nothing_to_save",
                payload={"ignored": ignored},
            )
        for env_name, value in updates.items():
            problem = _check_required(env_name, value)
            if problem:
                raise ConsoleError(problem, code="bad_value", payload={"field": env_name})

        before = set(self.settings.validate())
        text = self.env_file.read_text(encoding="utf-8") if self.env_file.is_file() else ""
        written = render_env(text, updates)
        candidate = self._candidate_settings(written)
        fresh = [item for item in candidate.validate() if item not in before]
        if fresh:
            raise ConsoleError(
                "配置不合法，.env 没有被改动：" + "；".join(fresh),
                code="invalid_config",
                payload={"errors": fresh},
            )

        core_files.atomic_write_text(self.env_file, written)
        restart_required = candidate.data_dir != self.settings.data_dir
        self._apply(candidate)
        return {
            "ok": True,
            "namespace": namespace,
            "written": sorted(updates),
            "ignored": ignored,
            "errors": candidate.validate(),
            "restart_required": restart_required,
            "env_file": str(self.env_file),
            "config": self.config(),
        }

    def _candidate_settings(self, env_text: str) -> Settings:
        """用"写回去的文本"试算一份 Settings：不落盘就能先跑一遍校验。

        基线是**当前** Settings，而不是代码默认值：``.env`` 里没写的键（密钥来自
        环境变量、``data_dir`` 来自命令行）不该在试算时被当成"有人把它删了"——
        否则改一个历史轮数，也会被"YIXIANG_API_KEY 缺失"这种假警报拦下来。
        """
        raw_env = parse_env_text(env_text)
        candidate = Settings.load(
            env_file=None, environ=raw_env, project_root=self.settings.project_root
        )
        for field in fields(Settings):
            if field.name in {"project_root", "env_file"}:
                continue
            if ENV_PREFIX + field.name.upper() in raw_env:
                continue  # .env 里写了的键以 .env 为准
            setattr(candidate, field.name, getattr(self.settings, field.name))
        return candidate

    def _apply(self, candidate: Settings) -> None:
        """把新值贴回运行中的 Settings 对象（provider 持有它的引用，下一轮即生效）。"""
        for field in fields(Settings):
            if field.name in {"project_root", "env_file", "data_dir"}:
                continue  # data_dir 例外：连接已开在旧目录，要重启（见 restart_required）
            setattr(self.settings, field.name, getattr(candidate, field.name))


# --------------------------------------------------------------------- 工具函数
def mask_secret(value: str) -> str:
    """密钥掩码：``sk-1****f3eb``。太短的整串打掉，别把长度也漏出去。"""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) < 12:
        return "****"
    return f"{text[:4]}****{text[-4:]}"


def _clean_session_id(session_id: str) -> str:
    text = str(session_id or "").strip()
    if not text:
        raise ConsoleError("会话 id 是空的：从历史列表里点一个。", code="bad_session")
    if len(text) > SESSION_ID_LIMIT or any(ch.isspace() for ch in text):
        raise ConsoleError(
            f"会话 id 不合法（≤{SESSION_ID_LIMIT} 字符、不含空白）：{text[:40]!r}",
            code="bad_session",
        )
    return text


def _safe_upload_name(filename: str) -> str:
    """把上传文件名收敛成"没有目录、没有路径分隔符、没有怪字符"的一段。"""
    raw = str(filename or "").replace("\\", "/").split("/")[-1].strip()
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", raw).strip("-_.")
    if not cleaned:
        return "upload"
    if len(cleaned) > 60:  # 保头保尾，中间省略：扩展名不能丢
        cleaned = f"{cleaned[:44]}-{cleaned[-12:]}"
    return cleaned


def _unique_path(directory: Path, base: str) -> Path:
    """同名不覆盖：第二个变成 ``xxx-2.md``（上传两次同名文件不该丢第一份）。"""
    candidate = directory / base
    if not candidate.exists():
        return candidate
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    for index in range(2, 200):
        candidate = directory / f"{stem}-{index}{dot}{ext}"
        if not candidate.exists():
            return candidate
    raise ConsoleError(f"{directory} 里同名文件太多，换个文件名再传。", status=409)


def render_env(text: str, updates: dict[str, str]) -> str:
    """把 ``KEY=VALUE`` 合并进 .env 文本：命中的行**原地替换**，其余行（含注释）原样保留。

    行尾的 ``# 注释`` 会被留下——.env 是给人看的，改值不该顺手吃掉注释。
    """
    pending = dict(updates)
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        body = stripped[7:].lstrip() if stripped.lower().startswith("export ") else stripped
        key, sep, value = body.partition("=")
        key = key.strip()
        if not sep or key not in pending:
            continue
        comment = ""
        raw = value
        if raw[:1] not in {'"', "'"} and " #" in raw:
            # 行尾注释连它前面的空格一起留下：.env 是给人看的，对齐不该被改值打乱
            comment = raw[len(raw.split(" #", 1)[0].rstrip()) :]
        indent = line[: len(line) - len(line.lstrip())]
        lines[index] = f"{indent}{key}={pending.pop(key)}{comment}"
    if pending:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# ── 由 Web 控制台写入 ──")
        lines.extend(f"{key}={value}" for key, value in pending.items())
    return "\n".join(lines).rstrip("\n") + "\n"


def _env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip()


def _check_required(env_name: str, value: str) -> str | None:
    """必填项的空值校验（可行动：说清哪个键、为什么不能空）。"""
    if env_name == ENV_PREFIX + "MAIN_MODEL" and not value:
        return "YIXIANG_MAIN_MODEL 不能为空：主对话模型是唯一必填的角色。"
    if env_name == ENV_PREFIX + "API_BASE" and not value:
        return "YIXIANG_API_BASE 不能为空，例如 https://api.deepseek.com/v1"
    return None


def _qq_status(enabled: bool, allowed: list[str]) -> str:
    if enabled and allowed:
        return f"已开启（白名单 {len(allowed)} 个 QQ 号）"
    if enabled:
        return "配置不合法：开了网关但白名单为空（会被拒绝启动）"
    if allowed:
        return f"已关闭（已存白名单 {len(allowed)} 个，开启即生效）"
    return "已关闭（P2 才接网关）"


__all__ = [
    "BLOCK_LABELS",
    "CONFIG_FIELDS",
    "ConsoleAPI",
    "ConsoleError",
    "DEFAULT_SESSION_ID",
    "MAX_UPLOAD_BYTES",
    "PERSONA_FILES",
    "QQ_FIELDS",
    "UPLOADS_SUBDIR",
    "mask_secret",
    "render_env",
]
