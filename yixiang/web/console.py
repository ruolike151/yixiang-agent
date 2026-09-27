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

import contextlib
from collections.abc import Callable, Iterable
from dataclasses import fields
from pathlib import Path
from typing import Any

from yixiang.app import App
from yixiang.config import ENV_PREFIX, Settings, parse_env_text
from yixiang.memory import core_files, procedural, sync
from yixiang.memory.core_files import CHAR_LIMITS, MEMORY_MAX_LINES, LimitExceeded
from yixiang.ops.tracing import find_trace, read_traces
from yixiang.ops.usage import summarize
from yixiang.runtime.eventloop import CancelToken
from yixiang.runtime.media import MAX_INLINE_IMAGE_BYTES, sniff_image_file
from yixiang.runtime.models import LoopEvent
from yixiang.runtime.session import CONTEXT_BUDGET_CHARS, read_core_file
from yixiang.runtime.uploads import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_LABEL,
    UPLOADS_SUBDIR,
    store_upload,
)

# 取不回类型时的兜底（图片走 sniff_image_file 认出真实类型）
OCTET_STREAM = "application/octet-stream"
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
    "judge_api_base",
    "judge_api_key",
    "no_think_models",
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
SECRET_FIELDS = ("api_key", "judge_api_key", "qq_token")

# 下拉候选。模型名**不是白名单**（vLLM / Ollama 可以填任意名字），只作为建议值；
# 候选必须同时满足两条：能在 ``ops/pricing.py`` 里查到价（否则成本静默按兜底价计，偏乐观）、
# 名字还在官网上架（deepseek-chat / deepseek-reasoner 已下架，见 ``config.RETIRED_MODELS``）。
# 官网在售的另一个是 deepseek-v4-pro，但它没进价目表，所以不摆进候选。
MODEL_SUGGESTIONS = ("deepseek-flash",)
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
                "judge_base": settings.judge_base,
                "judge_api_key_set": bool(settings.judge_auth_key),
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

    def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        """改会话名；``title`` 为空 = 恢复默认标题（首条用户消息前 60 字）。"""
        self.app.session.rename_session(_clean_session_id(session_id), _clean_title(title))
        return self.sessions()

    def delete_session(self, session_id: str) -> dict[str, Any]:
        """删一个会话的往来记录（不含长期记忆）。正在用的那个不许删。"""
        target = _clean_session_id(session_id)
        if target == self.app.session.session_id:
            raise ConsoleError(
                "不能删掉正在用的会话：先切到别的会话再删。",
                code="session_in_use",
            )
        removed = self.app.session.delete_session(target)
        return self.sessions() | {"removed": removed}

    def search_sessions(self, query: str, limit: int = 30) -> dict[str, Any]:
        """搜历史会话：形状与 ``sessions()`` 一致，前端可以把结果直接当列表用。"""
        session = self.app.session
        text = str(query or "").strip()
        return {
            "current": session.session_id,
            "query": text,
            "sessions": session.search_sessions(text, limit=max(int(limit), 1)),
        }

    def export_session(self, session_id: str) -> dict[str, Any]:
        """导出某个会话的全部往来（前端拼成 Markdown 让浏览器下载）。"""
        target = _clean_session_id(session_id)
        payload = self.app.session.export_session(target)
        if not payload["turns"]:
            raise ConsoleError(
                f"会话 {target} 没有任何往来记录，没什么可导出的。",
                status=404,
                code="empty_session",
            )
        return payload

    # ------------------------------------------------------------------ 链路
    def traces(self, limit: int = 50) -> dict[str, Any]:
        """最近的若干轮（默认 50）：只给"一眼能扫"的字段，详情另有接口。"""
        records = read_traces(self.settings.traces_dir, limit=max(int(limit), 1))
        rows = [
            {
                "turn_id": record.get("turn_id", ""),
                "ts": record.get("ts", ""),
                "session": record.get("session", ""),
                "source": record.get("source", ""),
                "iterations": record.get("iterations", 1),
                "tokens": record.get("tokens") or {},
                "cost_cny": float(record.get("cost_cny") or 0.0),
                "tools": [str(call.get("tool")) for call in (record.get("tool_calls") or [])],
                "finish_reason": record.get("finish_reason", ""),
                "error": record.get("error"),
                # 检索降级状态（D-24）：前端据此点亮"已降级（纯 FTS5）"那一行
                "rag": record.get("rag") or {},
            }
            for record in records
        ]
        return {"count": len(rows), "traces": rows}

    def trace(self, turn_id: str) -> dict[str, Any]:
        """某一轮的完整记录（字段与 ``yixiang ops show-trace`` 同源）。"""
        wanted = str(turn_id or "").strip()
        if not wanted:
            raise ConsoleError("turn_id 是空的：从列表里点一条。", code="bad_turn")
        record = find_trace(self.settings.traces_dir, wanted)
        if record is None:
            raise ConsoleError(
                f"找不到这条 trace：{wanted}（可能在别的日期、或这台机器还没聊过）",
                status=404,
                code="no_trace",
            )
        return record

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
        # 密钥明文永不出站：要改就填新的，留空 = 这次不改
        values["api_key"] = ""
        values["judge_api_key"] = ""
        return {
            "fields": values,
            "secret_mask": {
                "api_key": mask_secret(settings.api_key),
                "judge_api_key": mask_secret(settings.judge_auth_key),
            },
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
            "note": (
                "这里的配置会写进 .env；QQ 网关由 `yixiang serve` 加载，改完重启该进程生效。"
                "默认监听 127.0.0.1:8766——8765 留给 Web 控制台，两个都开时不会撞端口。"
            ),
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
        """把上传件落到 ``data/uploads/``：图片能随消息发出去，文本用 read_file 读。"""
        if not data:
            raise ConsoleError("文件是空的：选一个非空文件再传。", code="empty_file")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ConsoleError(
                f"文件 {len(data)} 字节，超过上限 {MAX_UPLOAD_BYTES} 字节（{MAX_UPLOAD_LABEL}）；"
                "先截断，或转成更小的 .md / .txt 再传。",
                status=413,
                code="too_large",
                payload={"limit": MAX_UPLOAD_BYTES, "actual": len(data)},
            )
        # 落盘口径与 QQ 网关共用一份（runtime/uploads.py）：日期打头、同名让位
        directory = self.settings.data_dir / UPLOADS_SUBDIR
        try:
            target = store_upload(
                data, directory=directory, filename=filename, now=self.app.clock.now()
            )
        except FileExistsError as exc:  # 同一个名字撞了 200 次：名字本身不对劲
            raise ConsoleError(str(exc), status=409, code="too_many_duplicates") from exc
        return self._upload_item(target) | {"dir": str(directory)}

    def list_uploads(self) -> dict[str, Any]:
        """``data/uploads/`` 里**现在**有什么——不是"这一次传了什么"。

        列表必须来自己磁盘：只记在浏览器内存里的话，刷新一次 store 就空了，而
        磁盘上的文件还在，界面上连一行都没有，用户自然无从删起（工作区只会越堆
        越满）。按文件名倒序 = 新的在前：上传件一律 ``YYYY-MM-DD-`` 打头，
        日期本身就是序号，上一版留下的老文件也在同一条队列里。
        """
        directory = self.settings.data_dir / UPLOADS_SUBDIR
        items: list[dict[str, Any]] = []
        if directory.is_dir():
            for entry in sorted(directory.iterdir(), key=lambda item: item.name, reverse=True):
                if not entry.is_file():
                    continue
                try:
                    items.append(self._upload_item(entry))
                except OSError:
                    continue  # 刚好被删掉 / 读不到：跳过，别让整个列表 500
        # ``limit`` 是"能传多大"，``inline_limit`` 是"多大以内模型真能看到像素"：
        # 两条上限不一样，界面上的说法就得从服务端拿，别在前端各写一遍数字。
        return {
            "items": items,
            "limit": MAX_UPLOAD_BYTES,
            "inline_limit": MAX_INLINE_IMAGE_BYTES,
            "dir": str(directory),
        }

    def delete_upload(self, name: str) -> dict[str, Any]:
        """删掉一个上传件：返回删掉的名字 + 剩下的列表（界面拿它直接重画）。"""
        target = self._upload_file(name)
        try:
            target.unlink()
        except OSError as exc:
            raise ConsoleError(
                f"删不掉 {target.name}：{exc}（文件可能正被别的程序占用）",
                status=500,
                code="delete_failed",
            ) from exc
        return {"deleted": target.name, "items": self.list_uploads()["items"]}

    def clear_uploads(self) -> dict[str, Any]:
        """一键清空 ``data/uploads/``：返回删掉几个（0 是结果，不是错误）。"""
        directory = self.settings.data_dir / UPLOADS_SUBDIR
        removed = 0
        if directory.is_dir():
            for entry in sorted(directory.iterdir()):
                if not entry.is_file():
                    continue
                try:
                    entry.unlink()
                except OSError as exc:
                    raise ConsoleError(
                        f"删不掉 {entry.name}：{exc}（文件可能正被别的程序占用）",
                        status=500,
                        code="delete_failed",
                    ) from exc
                removed += 1
        return {"deleted": removed, "items": []}

    def read_upload(self, name: str) -> tuple[bytes, str]:
        """取回一个上传件的字节 + 真实类型（图片缩略图那条路走它）。"""
        target = self._upload_file(name)
        return target.read_bytes(), (sniff_image_file(target) or OCTET_STREAM)

    # ------------------------------------------------------------------ 对话
    def chat(
        self,
        text: str,
        emit: Emit | None = None,
        *,
        images: Iterable[str] | None = None,
        token: CancelToken | None = None,
    ) -> dict[str, Any]:
        """跑一轮（流式）：事件实时喂给 ``emit``，返回值是这一轮的结果快照。

        ``token`` 由 Web 层给：它是「停止生成」唯一的落点（``None`` = 不可取消）。

        ``images`` 是本轮附图（工作区里已经存在的图片路径）。只带图不带字是合法
        的一轮——"看看这张"这句话本身可以省掉；两样都空才算空消息。
        """
        message = (text or "").strip()
        picked = self._pick_images(images)
        if not message and not picked:
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
            # 走 App.ask（本线程常驻 loop）：每轮新建再关掉 loop 会让 provider 缓存的
            # 连接池在第二轮报 Event loop is closed
            result = self.app.ask(
                message, observer=observer, stream=True, token=token, images=picked or None
            )
        except Exception as exc:  # noqa: BLE001 - 如实回错，不让 500 页面吞掉原因
            raise ConsoleError(f"这一轮没能跑完：{exc}", status=500, code="turn_failed") from exc
        payload = {
            "turn_id": result.turn_id,
            "reply": result.reply,
            "model": result.model,
            "finish_reason": result.finish_reason,
            "cancelled": result.finish_reason == "cancelled",
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

    def _upload_item(self, target: Path) -> dict[str, Any]:
        """一个上传件的界面口径：是图就给缩略图地址，是普通文件就给 read_file 提示。

        图和文件走两条路，所以 ``kind`` 由**内容**决定（``sniff_image_file`` 只读
        文件头）：图片是"随消息发出去"（``read_file`` 读不了二进制，给它那个提示
        只会让模型拿到一屏乱码），普通文件才是"让模型读它"。
        """
        relative = f"{UPLOADS_SUBDIR}/{target.name}"
        mime = sniff_image_file(target)
        return {
            "path": relative,
            "name": target.name,
            "bytes": target.stat().st_size,
            "kind": "image" if mime else "file",
            "mime": mime,
            "url": f"/api/uploads/{target.name}/raw",
            "hint": "" if mime else _read_file_hint(relative),
        }

    def _upload_file(self, name: str) -> Path:
        """把界面给的名字收敛成 ``data/uploads/`` 里的一个真实文件。

        只认**纯文件名**：带目录分隔符、``..``、绝对路径的一律按"没有这个文件"回
        ——越界要在这里就断掉，而不是等 ``Path`` 拼出一个能碰到 ``data/`` 其它地方
        的路径（``delete_upload`` / ``read_upload`` 都只有这一道门）。
        """
        cleaned = str(name or "").strip()
        if not cleaned or cleaned in {".", ".."} or Path(cleaned).name != cleaned:
            raise _upload_missing(cleaned)
        target = self.settings.data_dir / UPLOADS_SUBDIR / cleaned
        if not target.is_file():
            raise _upload_missing(cleaned)
        return target

    def _pick_images(self, images: Iterable[str] | None) -> list[str]:
        """校验本轮附图并归一成 ``uploads/<name>``：不存在 / 不是图都当场回错。

        静默丢掉一张图比报错更糟：用户以为模型"看过"了，其实它只看到一行路径，
        于是"看图"的结论无从核对。所以这里宁可 400。
        """
        picked: list[str] = []
        for item in images or ():
            raw = str(item or "").strip().replace("\\", "/")
            if raw.startswith(f"{UPLOADS_SUBDIR}/"):
                raw = raw[len(UPLOADS_SUBDIR) + 1 :]  # 前端可能带上前缀，也可能不带
            try:
                target = self._upload_file(raw)
            except ConsoleError as exc:
                raise _bad_image(str(item)) from exc
            if not sniff_image_file(target):
                raise _bad_image(f"{UPLOADS_SUBDIR}/{target.name}")
            picked.append(f"{UPLOADS_SUBDIR}/{target.name}")
        return picked

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


def _clean_title(value: str) -> str:
    """标题会直接进 DOM 与导出文件的第一行：先把换行 / 连续空格压平（长度由
    ``SessionManager.rename_session`` 按 TITLE_LIMIT 截断，这里不抄第二个上限）。"""
    return " ".join(str(value or "").split())


def _read_file_hint(relative: str) -> str:
    """普通上传件的提示语：点一下就填进输入框，模型随即用 ``read_file`` 去读。"""
    return f'让我读它：read_file(path="{relative}")'


def _upload_missing(name: str) -> ConsoleError:
    """工作区里没有这个上传件（含越界名字）：404，且不透露 ``data/`` 的布局。"""
    return ConsoleError(
        f"工作区里没有这个文件：{name or '（空名字）'}（可能已经被删掉了）",
        status=404,
        code="upload_not_found",
    )


def _bad_image(relative: str) -> ConsoleError:
    """附图不是"工作区里的一张图片"：400，并把是哪一个说出来。"""
    return ConsoleError(
        f"发不了这张图：{relative}（只支持工作区里已有的图片，先上传再发送）",
        status=400,
        code="bad_image",
    )


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
    return "已关闭（YIXIANG_QQ_ENABLED 未开）"


__all__ = [
    "BLOCK_LABELS",
    "CONFIG_FIELDS",
    "ConsoleAPI",
    "ConsoleError",
    "DEFAULT_SESSION_ID",
    "MAX_UPLOAD_BYTES",
    "MAX_UPLOAD_LABEL",
    "PERSONA_FILES",
    "QQ_FIELDS",
    "UPLOADS_SUBDIR",
    "mask_secret",
    "render_env",
]
