"""QQ 网关：OneBot v11 反向 WebSocket（TECH §10.2、PART-4 附录 A-2 / D-13）。

方向先说清：**反向 WS 是 NapCat 主动连我们**（本机没有公网 IP 也能收消息），所以这里
起的是**服务端**（``websockets.asyncio.server.serve``），不是客户端。

分层纪律（ADR-3）：网关只做协议转换与文本搬运。"解析 / 白名单 / 幂等 / 分片 / 限流"
全是**同步纯函数**，只有 ``serve_forever`` 那几行是 IO——所以 D-13（同一条
``message_id`` 只回一次）能在用例里直接跑，不用起真的 WebSocket。

安全默认（§10.2.5）：白名单为空 = 拒绝一切；群消息默认忽略；错误回复走统一模板，
不回显异常栈与文件系统路径。

**本任务不做**：图片段不下载（降级成 ``[图片]`` 占位）、多连接不做去重表。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed

from yixiang.app import App
from yixiang.config import Settings
from yixiang.runtime.models import Clock, SystemClock
from yixiang.runtime.session import SessionManager
from yixiang.tools.registry import Deps, ToolRegistry, build_registry

# 默认端口与 ``config.qq_listen`` 一致（8766：8765 留给 Web 控制台，见 Task 3）
DEFAULT_PORT = 8766
REPLY_CHUNK_CHARS = 1200
RATE_LIMIT_PER_MINUTE = 20
RECONNECT_INITIAL_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 60.0
PROCESSED_RETENTION_DAYS = 7
MAX_FRAME_BYTES = 1_000_000
CLOSE_POLICY_VIOLATION = 1008

AUDIT_DIRNAME = "logs"
AUDIT_FILENAME = "qq-audit.jsonl"

RATE_LIMIT_NOTICE = "你发得太快了，我先把这条记下，稍后再说。"
ERROR_NOTICE = "这一条我没处理成功，稍后再试试。"

_CQ_CODE = re.compile(r"\[CQ:[^\]]*\]")


# ------------------------------------------------------------------ 配置解析
def parse_listen(value: str) -> tuple[str, int]:
    """``127.0.0.1:8766`` → ``("127.0.0.1", 8766)``。

    配错了要当场炸：静默回落成默认端口会让"我以为它监听在 8766"变成一次
    "为什么手机发不出去"的深夜排查。
    """
    host, _, port = (value or "").strip().rpartition(":")
    if not host:
        raise ValueError(f"YIXIANG_QQ_LISTEN 需要 host:port 形式，收到 {value!r}")
    try:
        return host, int(port)
    except ValueError as exc:
        raise ValueError(f"YIXIANG_QQ_LISTEN 的端口不是数字：{value!r}") from exc


def parse_allowlist(raw: str) -> frozenset[str]:
    """逗号 / 分号 / 空白分隔的 QQ 号白名单；空 = 拒绝一切（§10.2.5-1）。"""
    parts = re.split(r"[,;\s]+", (raw or "").strip())
    return frozenset(part for part in parts if part)


def is_allowed(user_id: str, *, allowed: frozenset[str]) -> bool:
    """白名单是**精确匹配**：不做前缀、不做区间（少一个"看起来能进"的漏洞）。"""
    return str(user_id) in allowed


# ------------------------------------------------------------------ 事件解析
def strip_cq(text: str) -> str:
    """剥掉 CQ 码外壳（``[CQ:at,qq=…]`` 等），剩下的才是人话。"""
    return _CQ_CODE.sub("", text or "").strip()


def segments_to_text(message: Any) -> str:
    """OneBot ``message`` 段数组 → 纯文本。

    ``text`` 段拼接；``image`` 段降级成 ``[图片]`` 占位；face / at / reply 等
    其余段忽略——它们在聊天里是语气词，进 prompt 只会占预算。
    """
    if isinstance(message, str):
        return strip_cq(message)
    if not isinstance(message, list):
        return ""
    chunks: list[str] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        data = segment.get("data") or {}
        match segment.get("type"):
            case "text":
                chunks.append(str(data.get("text", "")))
            case "image":
                chunks.append("[图片]")
    return strip_cq("".join(chunks))


@dataclass(frozen=True, slots=True)
class Incoming:
    """一条通过入口校验的私聊消息（``group_id`` 非空 = 开了群消息开关）。"""

    message_id: str
    user_id: str
    text: str
    group_id: str = ""


def parse_event(payload: Any, *, group_enabled: bool = False) -> Incoming | None:
    """事件 → ``Incoming``；任何"不该处理"的情况返回 ``None``（丢弃即安全默认）。"""
    if not isinstance(payload, dict) or payload.get("post_type") != "message":
        return None
    kind = str(payload.get("message_type") or "")
    group_id = str(payload.get("group_id") or "")
    if kind == "group":
        if not group_enabled:
            return None
    elif kind != "private":
        return None
    user_id = str(payload.get("user_id") or "")
    if not user_id:
        return None
    raw = payload.get("message")
    text = segments_to_text(raw if raw is not None else payload.get("raw_message", ""))
    if not text:
        return None
    return Incoming(
        message_id=str(payload.get("message_id") or ""),
        user_id=user_id,
        text=text,
        group_id=group_id if kind == "group" else "",
    )


# ------------------------------------------------------------------ 幂等表
def claim_message(conn: Any, message_id: str, *, now: datetime) -> bool:
    """占位插入；``True`` = 这条归我处理（重复投递返回 ``False``，§10.2.3）。

    用 ``ON CONFLICT DO NOTHING`` + ``rowcount``：一次写入就同时完成"判重"与"占位"，
    中间没有 check-then-act 的窗口。没有 ``message_id`` 时**不判重**——宁可多回一次，
    也不能把一条真消息静默吞掉。
    """
    if not message_id:
        return True
    with conn:
        cursor = conn.execute(
            "INSERT INTO processed_messages(message_id, received_at) VALUES(?, ?)"
            " ON CONFLICT(message_id) DO NOTHING",
            (message_id, now.isoformat()),
        )
    return int(cursor.rowcount or 0) == 1


def mark_handled(conn: Any, message_id: str, *, now: datetime) -> None:
    """处理完才写 ``handled_at``：只有这一列有值才算"真的回过话"。"""
    if not message_id:
        return
    with conn:
        conn.execute(
            "UPDATE processed_messages SET handled_at = ? WHERE message_id = ?",
            (now.isoformat(), message_id),
        )


def prune_processed(conn: Any, *, before: datetime) -> int:
    """清掉保留期之外的幂等记录（§10.2.3 的 7 天），返回删了几行。

    比较的是 ISO 字符串：写进来的都是同一个时区的 aware 时间，字典序 = 时间序。
    """
    with conn:
        cursor = conn.execute(
            "DELETE FROM processed_messages WHERE received_at < ?", (before.isoformat(),)
        )
    return int(cursor.rowcount or 0)


# ------------------------------------------------------------------ 分片与限流
def split_reply(text: str, *, limit: int = REPLY_CHUNK_CHARS) -> list[str]:
    """按段落边界分片（§10.2.4）：不切在句子中间，多片时加 ``(i/n)`` 序号。"""
    body = (text or "").strip()
    if not body:
        return []
    if len(body) <= limit:
        return [body]
    chunks: list[str] = []
    current = ""
    for line in body.splitlines(keepends=True):
        if current and len(current) + len(line) > limit:
            chunks.append(current.rstrip())
            current = ""
        while len(line) > limit:  # 单行本身就超长：硬切，但不丢字
            chunks.append(line[:limit])
            line = line[limit:]
        current += line
    if current.strip():
        chunks.append(current.rstrip())
    if len(chunks) <= 1:
        return chunks or [body]
    total = len(chunks)
    return [f"({index}/{total}) {part}" for index, part in enumerate(chunks, start=1)]


class RateLimiter:
    """单用户滑动窗口限流（§10.2.4：默认 20 条/分钟）。"""

    def __init__(
        self,
        *,
        per_minute: int = RATE_LIMIT_PER_MINUTE,
        window: timedelta = timedelta(minutes=1),
    ) -> None:
        self.per_minute = per_minute
        self.window = window
        self._seen: dict[str, deque[datetime]] = {}

    def allow(self, user_id: str, now: datetime) -> bool:
        bucket = self._seen.setdefault(user_id, deque())
        cutoff = now - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.per_minute:
            return False
        bucket.append(now)
        return True


def _loads(raw: Any) -> Any:
    """坏帧当没收到：网关不因为对面发了一段垃圾就断线。"""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


class QQGateway:
    """把 OneBot 事件搬进 ``App.handle_message``（网关层不做业务，ADR-3）。"""

    def __init__(
        self,
        app: App,
        *,
        settings: Settings,
        conn: Any,
        clock: Clock | None = None,
        listen: str | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.app = app
        self.settings = settings
        self.conn = conn
        self.clock = clock or SystemClock()
        self.listen = listen or settings.qq_listen
        self.limiter = limiter or RateLimiter()
        self.sleep = sleep
        self.allowed = parse_allowlist(settings.qq_allowed)
        # 外部来源收窄工具面：这份注册表**只**给 QQ 用（§10.2.5-4）
        self.registry: ToolRegistry = build_registry(
            settings,
            Deps(
                conn=conn,
                clock=self.clock,
                data_dir=settings.data_dir,
                source="qq",
            ),
        )
        self._sessions: dict[str, SessionManager] = {}

    # -------------------------------------------------------------- 会话
    def session_for(self, user_id: str) -> SessionManager:
        """一个 QQ 号一条会话（``qq:<user_id>``），懒建并一直复用。"""
        key = f"qq:{user_id}"
        session = self._sessions.get(key)
        if session is None:
            session = self.app.new_session_manager(key, source="qq")
            self._sessions[key] = session
        return session

    # -------------------------------------------------------------- 握手
    def handshake_ok(self, connection: Any) -> bool:
        """握手校验（§10.2.1）：没配 token 就放行；配了要求 Bearer 或查询参数匹配。"""
        token = (self.settings.qq_token or "").strip()
        if not token:
            return True
        request = getattr(connection, "request", None)
        headers = getattr(request, "headers", None)
        if headers is not None and str(headers.get("Authorization", "")) == f"Bearer {token}":
            return True
        return f"access_token={token}" in str(getattr(request, "path", "") or "")

    # -------------------------------------------------------------- 一条消息
    async def handle_event(self, payload: Any) -> list[str]:
        """一条事件 → 要发回去的文本分片（``[]`` = 什么都不发）。

        顺序是**冻结契约**：解析 → 白名单 → 限流 → 幂等占位 → 跑链路 → 分片。
        先占位再跑，是为了让"跑挂了"也不会在重连后被重跑（重连补偿由晨报负责，
        不靠把用户的话再说一遍）。
        """
        incoming = parse_event(payload, group_enabled=self.settings.qq_group_enabled)
        if incoming is None:
            return []
        now = self.clock.now()
        if not is_allowed(incoming.user_id, allowed=self.allowed):
            self._audit("rejected", incoming=incoming)
            return []
        if not self.limiter.allow(incoming.user_id, now):
            self._audit("rate_limited", incoming=incoming)
            return [RATE_LIMIT_NOTICE]
        if not claim_message(self.conn, incoming.message_id, now=now):
            self._audit("duplicate", incoming=incoming)
            return []
        try:
            result = await self.app.handle_message(
                incoming.text,
                session=self.session_for(incoming.user_id),
                stream=False,
                registry=self.registry,
            )
        except Exception as exc:  # 网关不许把异常栈回给用户（§10.2.5-5）
            self._audit("error", incoming=incoming, detail=f"{type(exc).__name__}: {exc}")
            return [ERROR_NOTICE]
        mark_handled(self.conn, incoming.message_id, now=self.clock.now())
        if result.error:
            self._audit("model_error", incoming=incoming, detail=str(result.error))
            if not result.reply.strip():
                return [ERROR_NOTICE]
        # 本轮"有失败"不等于"答案不可用"：loop 已经把失败原因作为 notice 拼在 reply
        # 末尾（agent.py 的 ``if any(not event.ok ...)`` 那段），有答案就照发。
        # 工具偶发失败一次、模型换参数重试成功——这时丢掉整条答案只回"没处理成功"，
        # 是 2026-09-22 实测到的现象。
        return split_reply(result.reply)

    # -------------------------------------------------------------- 审计
    def _audit(self, event: str, *, incoming: Incoming | None = None, detail: str = "") -> None:
        """审计日志（§10.2.1：拒收与限流要留痕）。写不进去也不许影响收消息。"""
        entry: dict[str, Any] = {
            "ts": self.clock.now().isoformat(),
            "event": event,
            "detail": detail,
        }
        if incoming is not None:
            entry["user_id"] = incoming.user_id
            entry["message_id"] = incoming.message_id
        path = self.settings.data_dir / AUDIT_DIRNAME / AUDIT_FILENAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # -------------------------------------------------------------- 收连接
    async def serve_forever(self) -> None:
        """反向 WS 服务端：等 OneBot 连过来，监听失败按 1→2→4→…→60s 退避重试。

        这里是**服务端**：重连的一方是 NapCat。我们这层的"重连"只针对自己
        ``bind`` 失败（端口被占 / 网卡没了），退避封顶 60 秒，永不放弃。
        """
        host, port = parse_listen(self.listen)
        backoff = RECONNECT_INITIAL_SECONDS
        while True:
            try:
                async with ws_serve(self._on_connection, host, port, max_size=MAX_FRAME_BYTES):
                    self._audit("listening", detail=f"{host}:{port}")
                    backoff = RECONNECT_INITIAL_SECONDS
                    await asyncio.Future()  # 挂住；被取消（Ctrl+C / 测试）才退出
            except asyncio.CancelledError:
                raise
            except OSError as exc:
                self._audit("listen_failed", detail=f"{type(exc).__name__}: {exc}")
                await self.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX_SECONDS)

    async def _on_connection(self, connection: Any) -> None:
        if not self.handshake_ok(connection):
            self._audit("handshake_rejected", detail=str(getattr(connection, "remote_address", "")))
            await connection.close(code=CLOSE_POLICY_VIOLATION)
            return
        self._audit("connected", detail=str(getattr(connection, "remote_address", "")))
        try:
            async for raw in connection:
                payload = _loads(raw)
                chunks = await self.handle_event(payload)
                user_id = str((payload or {}).get("user_id") or "")
                for chunk in chunks:
                    await self._send(connection, user_id, chunk)
        except ConnectionClosed:  # 对面先走是常态：等它回连，不记失败
            pass

    async def _send(self, connection: Any, user_id: str, text: str) -> None:
        """发一条私聊消息：失败重试 1 次，仍失败只记审计（§10.2.4）。"""
        call = json.dumps(
            {
                "action": "send_private_msg",
                "params": {
                    "user_id": int(user_id) if user_id.isdigit() else user_id,
                    "message": [{"type": "text", "data": {"text": text}}],
                },
            },
            ensure_ascii=False,
        )
        for attempt in (1, 2):
            try:
                await connection.send(call)
                return
            except Exception as exc:
                self._audit(
                    "send_failed",
                    detail=f"第 {attempt} 次：{type(exc).__name__}: {exc}",
                )
