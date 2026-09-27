"""QQ 网关：OneBot v11 反向 WebSocket（TECH §10.2、PART-4 附录 A-2 / D-13）。

方向先说清：**反向 WS 是 NapCat 主动连我们**（本机没有公网 IP 也能收消息），所以这里
起的是**服务端**（``websockets.asyncio.server.serve``），不是客户端。

分层纪律（ADR-3）：网关只做协议转换与素材搬运。"解析 / 白名单 / 幂等 / 分片 / 限流"
全是**同步纯函数**，只有 ``serve_forever`` 与收图那几步是 IO——所以 D-13（同一条
``message_id`` 只回一次）能在用例里直接跑，不用起真的 WebSocket。

安全默认（§10.2.5）：白名单为空 = 拒绝一切；群消息默认忽略；错误回复走统一模板，
不回显异常栈与文件系统路径。

图片段走的是和 Web 上传**同一条**多模态通路：下载到 ``data/uploads/``，把相对路径
交给 ``App.handle_message(images=…)``，像素由 Provider 内联成 content part。图片段
只带 URL（``enableLocalFile2Url: false`` 时 NapCat 给的 ``data.url`` 就是唯一的取处），
所以下载要限时限量（``IMAGE_FETCH_TIMEOUT`` / ``image_limit``）；取不到就把"有 N 张图
没取到"如实拼进这一轮（``IMAGE_MISSED``）——**不许假装看见过**。

**本任务不做**：多连接不做去重表；群消息里的图不下载（群消息本身就默认忽略）。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed

from yixiang.app import App
from yixiang.config import Settings
from yixiang.runtime.media import extension_for, sniff_image_mime
from yixiang.runtime.models import Clock, SystemClock
from yixiang.runtime.session import SessionManager
from yixiang.runtime.uploads import MAX_UPLOAD_BYTES, UPLOADS_SUBDIR, store_upload
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

# 只有图、还没有字的那一条：回一句短的，然后**不花模型的钱**（2026-09-27 决定：
# 完全安静最容易让人以为又坏了，而"先发图再打字"是 QQ 上的常态）
IMAGE_ACK = "图收到了，你想问什么？"
IMAGE_FAILED_NOTICE = "这张图我没取到（可能是过期了），再发一次或者直接说文字吧。"
# 图 + 话一起来、图却没取到时接在文本后面的一句：模型要知道自己漏了一张图
IMAGE_MISSED = "（有 {count} 张图没取到，可能已经过期）"

# 先到的图替下一条文字消息留着：10 张封顶、5 分钟作废（用户拍板的口径）
PENDING_IMAGE_LIMIT = 10
PENDING_IMAGE_TTL = timedelta(minutes=5)

# 单张图的下载超时：QQ 图片在腾讯 CDN 上，正常一两秒；卡住就别拖着整条消息
IMAGE_FETCH_TIMEOUT = 20.0

_CQ_CODE = re.compile(r"\[CQ:[^\]]*\]")
# ``messagePostFormat: "string"`` 时图藏在 CQ 码里，参数表就是这一段
_CQ_IMAGE_PARAMS = re.compile(r"\[CQ:image,([^\]]*)\]")


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


def image_source(data: Any) -> str:
    """图片段 → 取字节的地址；两处都没有就返回空串。

    ``url`` 优先：它是 OneBot 给图的标准取处（NapCat 默认
    ``enableLocalFile2Url: false``，``file`` 可能只是一串缓存里的 hash）。
    退回 ``file`` 是为了 ``file://`` / 本机绝对路径那种配法，见 ``read_local_image``。
    """
    if not isinstance(data, dict):
        return ""
    return str(data.get("url") or data.get("file") or "").strip()


def _local_path(source: str) -> Path:
    """``file://`` / 本机路径 → ``Path``（Windows 盘符那个易错点收在这一个地方）。

    ``urlparse("E:/x.png")`` 会把 ``E`` 当成 scheme（单字母 scheme 的经典坑），所以
    只有在真的带 ``file:`` 前缀时才走 URI 解析，其余一律当本机路径原样交给 ``Path``。
    """
    if not source.lower().startswith("file:"):
        return Path(source)
    parsed = urlparse(source)
    segment = unquote(parsed.path)
    if parsed.netloc:  # file://server/share/x 这种 UNC 写法
        segment = f"//{parsed.netloc}{segment}"
    # file:///E:/x.png 解析出 ``/E:/x.png``：那个前导斜杠是 URI 的，不是路径的
    if len(segment) > 2 and segment[0] == "/" and segment[2] == ":":
        segment = segment[1:]
    return Path(segment)


def read_local_image(source: str) -> bytes | None:
    """本机路径 / ``file://`` → 字节；http(s) 与读不到的一律返回 ``None``。

    分出来是为了让"取图"这件事的**网络那一步**只有一个入口：NapCat 打开
    ``enableLocalFile2Url`` 时 ``data.file`` 是本机路径，关着时才是 http。默认配置
    是关的，但两种配法都在真机上见过，所以两条都要认。
    """
    raw = (source or "").strip()
    if not raw or raw.lower().startswith(("http://", "https://")):
        return None
    try:
        return _local_path(raw).read_bytes()
    except OSError:
        return None


def _image_filename(source: str, mime: str) -> str:
    """落盘用的原名：URL 尾巴那一段；没有扩展名时按文件头补一个。

    QQ 图片段的 ``file`` 常常只是一串缓存 hash，所以**名字来自地址、类型来自字节**：
    两边各管各的，谁也不冒充谁（扩展名只为人看着舒服，真类型永远是 magic bytes）。
    """
    if source.lower().startswith(("http://", "https://")):
        name = Path(urlparse(source).path).name
    else:
        name = Path(source).name
    name = name or "qq-image"
    if not Path(name).suffix:
        name = f"{name}{extension_for(mime)}"
    return name


def cq_images(message: str) -> tuple[str, ...]:
    """``messagePostFormat: "string"`` 的消息里，图藏在 CQ 码里：取 ``url``（退回 ``file``）。

    本机 NapCat 配的是 ``array``（``message`` 直接给段数组），但配置是别人手边随时能改的
    东西——只认段数组的话，换个格式就变成"图静默消失"，而这正是要修的那个现象。
    """
    sources: list[str] = []
    for params in _CQ_IMAGE_PARAMS.findall(message or ""):
        fields: dict[str, str] = {}
        for pair in params.split(","):
            key, _, value = pair.partition("=")
            fields[key.strip()] = value.strip()
        source = fields.get("url") or fields.get("file") or ""
        if source:
            sources.append(source)
    return tuple(sources)


def parse_segments(message: Any) -> tuple[str, tuple[str, ...]]:
    """OneBot ``message`` 段数组 → ``(纯文本, 图片的取处)``。

    ``text`` 段拼接成文本；``image`` 段**不再降级成 ``[图片]``**——它的地址单独
    返回，由网关去取像素（取不到时才是占位符）。face / at / reply 等其余段忽略：
    它们在聊天里是语气词，进 prompt 只会占预算。

    ``message`` 是**字符串**（``messagePostFormat: "string"``）时同理：先收 CQ 码里的
    ``[CQ:image,…]``，再把剩下的壳剥掉。
    """
    if isinstance(message, str):
        return strip_cq(message), cq_images(message)
    if not isinstance(message, list):
        return "", ()
    chunks: list[str] = []
    sources: list[str] = []
    for segment in message:
        if not isinstance(segment, dict):
            continue
        data = segment.get("data") or {}
        match segment.get("type"):
            case "text":
                chunks.append(str(data.get("text", "")))
            case "image":
                source = image_source(data)
                if source:
                    sources.append(source)
    return strip_cq("".join(chunks)), tuple(sources)


@dataclass(frozen=True, slots=True)
class Incoming:
    """一条通过入口校验的私聊消息（``group_id`` 非空 = 开了群消息开关）。

    ``images`` 是图**还没下载**的样子：地址不是路径——落盘要等网关去取。
    """

    message_id: str
    user_id: str
    text: str
    group_id: str = ""
    images: tuple[str, ...] = ()


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
    text, images = parse_segments(raw if raw is not None else payload.get("raw_message", ""))
    if not text and not images:
        return None
    return Incoming(
        message_id=str(payload.get("message_id") or ""),
        user_id=user_id,
        text=text,
        group_id=group_id if kind == "group" else "",
        images=images,
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


class PendingImages:
    """先到的图替**下一条文字消息**留着：按人分账、10 张封顶、5 分钟作废。

    "先发图、再打字"在 QQ 上是常态，而那两条是**两条独立消息**。如果图那条当场跑
    模型，答案只能对着一个空问题瞎猜，说完文字再说一遍等于白花一次钱；如果图那条
    只回执却不留图，下一条文字里模型就只看得到"鉴赏这个排版"——2026-09-27 的
    真实记录正是这个形状。所以图先存这儿，等文字到达时随它一起进模型。

    取走即清：同一张图不能进两轮（否则下一轮会莫名其妙地"又看到"上一张）。
    """

    def __init__(
        self, *, limit: int = PENDING_IMAGE_LIMIT, ttl: timedelta = PENDING_IMAGE_TTL
    ) -> None:
        self.limit = limit
        self.ttl = ttl
        self._stash: dict[str, tuple[datetime, list[str]]] = {}

    def add(self, user_id: str, paths: Iterable[str], *, now: datetime) -> None:
        """把这一批图挂到这个用户名下；超过上限时**丢旧的**（他刚发的一组才算数）。"""
        fresh = [str(item) for item in paths if str(item)]
        if not fresh:
            return
        _, kept = self._stash.get(user_id, (now, []))
        self._stash[user_id] = (now, (list(kept) + fresh)[-self.limit :])

    def take(self, user_id: str, *, now: datetime) -> list[str]:
        """取走这个用户的待用图（连带作废过期的）；取过就没了，不会进第二轮。"""
        entry = self._stash.pop(user_id, None)
        if entry is None:
            return []
        stored_at, paths = entry
        if now - stored_at > self.ttl:
            return []
        return list(paths)


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
        fetch: Callable[[str], Awaitable[bytes]] | None = None,
        image_limit: int = MAX_UPLOAD_BYTES,
    ) -> None:
        self.app = app
        self.settings = settings
        self.conn = conn
        self.clock = clock or SystemClock()
        self.listen = listen or settings.qq_listen
        self.limiter = limiter or RateLimiter()
        self.sleep = sleep
        # 取图的动作可注入：真机走 ``_download_image``（本机直读 / http 拉一次），
        # 用例里换成按剧本回字节的假下载器——离线也能钉死"收图"这条链路。
        self.fetch: Callable[[str], Awaitable[bytes]] = fetch or self._download_image
        self.image_limit = image_limit
        self.pending = PendingImages(limit=PENDING_IMAGE_LIMIT)
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

        顺序是**冻结契约**：解析 → 白名单 → 限流 → 幂等占位 → 收图 → 跑链路 → 分片。
        先占位再跑，是为了让"跑挂了"也不会在重连后被重跑（重连补偿由晨报负责，
        不靠把用户的话再说一遍）。

        图片有两种到法：和文字一起来的那条随它进模型；只有图的那条**不跑模型**，
        回一句短回执、把图替下一条文字留着（见 ``PendingImages``）。
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
        saved, missed = await self._collect_images(incoming.images, incoming=incoming, now=now)
        text = incoming.text.strip()
        if not text:
            return self._receipt_for_images_only(
                incoming, saved=saved, missed=missed, now=now
            )
        # 上一条"只有图"留下的图随这条文字进模型：QQ 上先发图再打字是常态
        stashed = self.pending.take(incoming.user_id, now=now)
        picked = (saved + stashed)[:PENDING_IMAGE_LIMIT] or None
        # 图没取到要说出来：模型答"鉴赏这个排版"时得知道自己漏了一张图
        turn_text = f"{text}\n{IMAGE_MISSED.format(count=missed)}" if missed else text
        try:
            result = await self.app.handle_message(
                turn_text,
                session=self.session_for(incoming.user_id),
                stream=False,
                registry=self.registry,
                images=picked,
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

    # -------------------------------------------------------------- 收图
    def _receipt_for_images_only(
        self, incoming: Incoming, *, saved: list[str], missed: int, now: datetime
    ) -> list[str]:
        """只有图、没有字：回一句短的，**不花模型的钱**（2026-09-27 拍板）。

        完全安静最容易让人以为"又坏了"；而单独跑一轮模型，答案只能对着空问题瞎猜。
        所以：图留下、话留到下一条，这一条只给个收据。一张都没取到时如实说。
        """
        if not saved:
            self._audit("image_only_failed", incoming=incoming, detail=f"漏了 {missed} 张")
            return [IMAGE_FAILED_NOTICE]
        self.pending.add(incoming.user_id, saved, now=now)
        mark_handled(self.conn, incoming.message_id, now=now)
        return [IMAGE_ACK]

    async def _collect_images(
        self, sources: tuple[str, ...], *, incoming: Incoming, now: datetime
    ) -> tuple[list[str], int]:
        """逐张取图并落盘；任何一张失败都只记一条审计，不打断这一轮。

        返回 ``(落盘后的相对路径, 没取到的张数)``。上限判在**下载之后**：QQ 给的
        地址不带长度，只有到了手里才量得出大小（Web 上传那条路也是取到才判）。
        """
        saved: list[str] = []
        missed = 0
        for source in sources:
            try:
                data = await self.fetch(source)
            except Exception as exc:  # 过期 / 断网 / 对面 404：一律降级成"没取到"
                missed += 1
                self._audit(
                    "image_failed", incoming=incoming, detail=f"{source}: {type(exc).__name__}"
                )
                continue
            if not data or len(data) > self.image_limit:
                missed += 1
                self._audit("image_rejected", incoming=incoming, detail=f"{source}: {len(data)} 字节")
                continue
            mime = sniff_image_mime(data)
            if not mime:  # 名字叫图、内容不是图：不写盘，也不塞给模型半张坏数据
                missed += 1
                self._audit("image_not_an_image", incoming=incoming, detail=source)
                continue
            try:
                target = store_upload(
                    data,
                    directory=self.settings.data_dir / UPLOADS_SUBDIR,
                    filename=_image_filename(source, mime),
                    now=now,
                )
            except OSError as exc:  # 含 FileExistsError（同名太多）：收图不该因它失败
                missed += 1
                self._audit(
                    "image_write_failed", incoming=incoming, detail=f"{type(exc).__name__}: {exc}"
                )
                continue
            saved.append(f"{UPLOADS_SUBDIR}/{target.name}")
        return saved, missed

    async def _download_image(self, source: str) -> bytes:
        """默认取图：本机路径直读，http(s) 走一次限时 GET（腾讯 CDN 常带跳转）。"""
        local = read_local_image(source)
        if local is not None:
            return local
        async with httpx.AsyncClient(timeout=IMAGE_FETCH_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(source)
            response.raise_for_status()
            return response.content

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
