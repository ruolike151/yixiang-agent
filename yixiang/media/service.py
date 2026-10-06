"""音乐中枢：把 SMTC / 本地 ``.lrc`` / LRCLib / 本机曲库拼成一份状态 JSON。

这一层只依赖 ``Settings`` 和三个注入点（``session_factory`` / ``lyrics_client`` / ``clock``），
所以既能在 ``pytest`` 里 0.1 秒跑完全套，也能在 Web 的请求线程里被随便调。

它**不碰** ``App``：没有 ``chat_log``、没有 ``usage.jsonl``、没有 trace、不进 ``ToolRegistry``、
不进 ``_SerialRunner`` 那条唯一工作线程。音频只在本机浏览器里播，服务器只发文件字节——
**曲目信息默认只上屏，不进模型上下文**。

三种"读不到"的区别（同 Task 3 的口径，前端据它决定显示什么）：

  * ``connection="disabled"``：``YIXIANG_MUSIC_ENABLED=0``，压根没开。
  * ``connection="offline"``：开了，但系统里没有正在放的会话，或 SMTC 读不了。
  * ``connection="connected"``：读到了一条真的"正在播放"。
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from yixiang.config import Settings
from yixiang.media.lrclib import (
    LrclibUnavailable,
    cache_path,
    fetch_lrclib,
    read_cache,
    write_cache,
)
from yixiang.media.lyrics import LyricLine, Track, load_local_lyrics
from yixiang.media.providers.base import ProviderLyrics
from yixiang.media.smtc import (
    SmtcUnavailable,
    read_current,
)

# 与 ``MusicService.control`` 重名，所以起个别名——ruff 的 isort 要求别名单独成句。
from yixiang.media.smtc import control as smtc_control

# 轮询间隔的下限：写 0 会把面板变成每秒四次的忙轮询，而"配置写错"和"服务卡死"
# 在浏览器里长一个样。
MIN_POLL_MS = 250

# 本机曲库认得的声音文件；别的后缀一律不列（列了也播不了）。
AUDIO_SUFFIXES = (".mp3", ".m4a", ".flac", ".ogg", ".wav")
AUDIO_URL_PREFIX = "/api/audio/"

# 一次读不到 SMTC **不算**"没在放"：QQ 音乐切歌、窗口最小化、会话重建的那一两秒里
# ``get_current_session()`` 会短暂回 None。这段窗口内继续回上一份状态，面板与歌词
# 舞台就一格都不闪；用户看到的"歌放到一半突然消失"正是闪的那一下。
HOLD_MS = 8_000

# 取词失败也缓存，但**不是永远**：LRCLib 一次握手超时（真机上见过 ``_ssl.c:993``）
# 不该让这首歌在这次进程里再也问不出词来。过了这个窗口，下一次 ``state()`` 重问。
LYRICS_ERROR_RETRY_MS = 60_000

# 能被反向写回播放器的动作；不在这张表里的词是调用方写错代码，直接 ValueError。
ACTIONS = ("play", "pause", "next", "previous", "seek")


@dataclass(frozen=True)
class LyricsOutcome:
    """一次取词的结果。**永远**有一个结果：查不到是 ``empty``，出错是 ``error``。

    三种结果都会进 ``MusicService`` 的内存缓存，所以同一首歌在同一个进程里只问一次。
    歌词是装饰、播放状态是主菜——``error`` 不会影响 ``connection`` 与进度条。

    ``karaoke_lyric`` 是**逐字**通道：只有 QQ 的 QRC 来源会填它（Task 7 解密后的
    原始明文串），其余来源恒为 ``""``。它不进 ``lines``、不参与逐句渲染，只是原样
    透给歌词舞台的 Folia 引擎（Task 10 的 ``stateToFoliaEvents``）。
    """

    status: str
    source: str
    lines: tuple[LyricLine, ...] = ()
    note: str = ""
    karaoke_lyric: str = ""


def lyrics_json(outcome: LyricsOutcome) -> dict[str, Any]:
    """歌词 → 参考项目契约（毫秒时间轴）。``note`` 只在有人话要说时才出现。

    ``karaokeLyric`` / ``hasKaraokeLyric`` **恒存在**（没有逐字时是 ``""`` / ``False``），
    这样前端读 ``state.lyrics.karaokeLyric`` 不用先判字段在不在。QRC 明文**原样透传**，
    不在这一层解析——解析是 Folia 引擎自己的事（Task 10 把它塞进
    ``{type:'qrc', qrcContent: karaokeLyric}`` 事件）。
    """

    karaoke_lyric = outcome.karaoke_lyric
    payload: dict[str, Any] = {
        "status": outcome.status,
        "source": outcome.source,
        "lines": [{"timeMs": line.time_ms, "text": line.text} for line in outcome.lines],
        "karaokeLyric": karaoke_lyric,
        "hasKaraokeLyric": bool(karaoke_lyric.strip()),
    }
    if outcome.note:
        payload["note"] = outcome.note
    return payload


def online_lyrics_outcome(
    provider_name: str, result: ProviderLyrics | None
) -> LyricsOutcome:
    """一家在线平台的取词结果 → ``LyricsOutcome``（与本地 / LRCLib 同一个形状）。

    **永远有结果**：没查到（含 QQ 密文坏了那条降级说明）是 ``empty`` 并把 ``note``
    带上；有词是 ``ready``。**逐字那条线要显式透传**：``ProviderLyrics.karaoke_lyric``
    → ``LyricsOutcome.karaoke_lyric``（Task 5 已经加好的字段），Folia 引擎（task-10）
    靠它渲染逐字。``word_by_word`` / ``is_pure_music`` 仍留在 provider 层——
    ``LyricsOutcome`` 不为它们加字段（加了就得同步 ``lyrics_json()`` 与 Task 1 的契约用例）。
    """

    if result is None or not result.lines:
        note = result.note if result is not None else ""
        return LyricsOutcome(status="empty", source=provider_name, note=note)
    return LyricsOutcome(
        status="ready",
        source=provider_name,
        lines=result.lines,
        note=result.note,
        karaoke_lyric=result.karaoke_lyric,
    )


def track_from_query(
    title: str, artist: str = "", album: str = "", duration_ms: int = 0
) -> Track:
    """从"歌名 / 歌手"拼一个 Track（在线搜索、手输曲目用）。

    SMTC 不给曲目 id，所以 id 自己算：四个字段的 sha1。同一首歌永远同一个 id，
    前端拿它算"这首歌用哪个渲染模式"的哈希（Task 8）。
    """

    key = "|".join((title, artist, album, str(duration_ms)))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return Track(
        id=f"query:{digest}",
        title=title,
        artist=artist,
        album=album,
        duration_ms=duration_ms,
    )


class MusicService:
    """一条链路的唯一入口：状态 / 控制 / 取词 / 本机曲库。

    三个注入点都有默认值，所以生产代码 ``MusicService(settings)`` 就够，用例则全给假的：
    ``session_factory``（假 SMTC 会话）、``lyrics_client``（假 LRCLib 端点）、``clock``（假时钟）。
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: Callable[[], Any] | None = None,
        lyrics_client: httpx.Client | None = None,
        online_lyrics: Callable[[Track], LyricsOutcome] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._lyrics_client = lyrics_client
        self._online_lyrics = online_lyrics
        self._clock = clock
        # 按 track.id 存"这首歌问过什么"与**什么时候问的**：ready / empty 一律命中，
        # error 只认 ``LYRICS_ERROR_RETRY_MS`` 之内的那一次（见上面那个常量的注释）。
        # 切歌换 id 自然失效；磁盘缓存（Task 2）只管已查到的好结果。
        self._lyrics_memory: dict[str, tuple[LyricsOutcome, int]] = {}
        # 上一份"读到了"的完整状态（``None`` = 这个进程还没读到过）。
        # 有两个用处：SMTC 短暂读不到时把它顶上去；舞台问进度时从它往前推。
        self._last: dict[str, Any] | None = None
        # ``(positionMs, sampledAtMs, isPlaying)``：舞台的 ``/api/spotify/progress``
        # 按秒级轮询，每次去问一遍 winrt 太重，用上一次的快照往前推就够。
        self._last_progress: tuple[int, int, bool] = (0, 0, False)

    # ------------------------------------------------------------------ 状态
    def state(self, *, track: Track | None = None) -> dict[str, Any]:
        """这一秒的完整状态。

        ``track`` 传了就是"本机点唱"（``connection="local"`` / ``source="local-audition"``）——
        参考项目的歌词舞台靠这两个字段判断"有没有在放"；不传就走 SMTC。
        """

        now_ms = int(self._clock() * 1000)
        poll = self._poll()
        if not self._settings.music_enabled:
            return self._blank("disabled", now_ms, poll)
        if track is not None:
            return {
                "ok": True,
                "connection": "local",
                "source": "local-audition",
                "track": _track_json(track),
                "playback": {
                    "isPlaying": True,
                    "positionMs": 0,
                    "sampledAtMs": now_ms,
                    "controls": [],
                },
                "lyrics": lyrics_json(self.resolve_lyrics(track)),
                "pollMs": poll,
            }

        try:
            snapshot = read_current(session_factory=self._session_factory)
        except SmtcUnavailable as exc:
            return self._hold_or_blank(now_ms, poll, note=f"SMTC 读取失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 会话失效时 winrt 抛的类型随版本变，一律当"这次没读到"
            return self._hold_or_blank(now_ms, poll, note=f"SMTC 读取异常：{exc}")
        if snapshot is None:
            return self._hold_or_blank(now_ms, poll)
        if not snapshot.track.title.strip():
            # QQ 音乐切歌 / 最小化那一两秒里，SMTC 有时会回一条"有会话、没元数据"的空壳：
            # 歌名一空，面板与歌词舞台就都不知道放的是哪一首（用户看到的"无法识别"）。
            # 与"没有会话"一样当缺口处理：先顶上一首，窗口过了才承认读不到。
            return self._hold_or_blank(
                now_ms, poll, note="SMTC 只回了空标题：这一秒读不出是哪一首。"
            )

        position = snapshot.position_ms
        if snapshot.duration_ms > 0:
            position = min(position, snapshot.duration_ms)
        payload = {
            "ok": True,
            "connection": "connected",
            "source": "smtc",
            "track": _track_json(snapshot.track),
            "playback": {
                "isPlaying": snapshot.status == "playing",
                "positionMs": position,
                "sampledAtMs": now_ms,
                "controls": list(snapshot.controls),
            },
            "lyrics": lyrics_json(self.resolve_lyrics(snapshot.track)),
            "pollMs": poll,
        }
        self._last = payload
        self._last_progress = (position, now_ms, payload["playback"]["isPlaying"])
        return payload

    def progress_ms(self) -> int:
        """现在大概放到哪了（毫秒）。

        歌词舞台在嵌入模式下按秒级打 ``/api/spotify/progress``；那个接口**不能**每次
        都去问一遍 SMTC（一次 winrt 往返几十毫秒，还全排在同一条路上），所以拿上一次
        ``state()`` 的快照按流逝往前推。暂停时就是那个位置本身。
        """

        position, sampled_at, playing = self._last_progress
        if not playing:
            return max(0, position)
        elapsed = max(0, int(self._clock() * 1000) - sampled_at)
        return max(0, position + elapsed)

    # ------------------------------------------------------------------ 控制
    def control(self, action: str, *, position_ms: int = 0) -> dict[str, Any]:
        """替用户按一下播放器的按钮。

        ``ok`` 恒为 ``True``：请求本身被听懂了。``applied=False`` 时 ``reason`` 说清是哪种
        "按不动"——``disabled``（音乐没开）/ ``smtc-unavailable``（系统读不了）/
        ``not-applied``（没会话、这个 app 没这个按钮、winrt 回了 ``False``）。
        只有动作名不在 ``ACTIONS`` 里才抛 ``ValueError``（那是调用方写错代码）。
        """

        if action not in ACTIONS:
            raise ValueError(f"不认识的控制动作：{action}")
        if not self._settings.music_enabled:
            return {"ok": True, "applied": False, "reason": "disabled"}
        try:
            applied = smtc_control(
                action, position_ms=position_ms, session_factory=self._session_factory
            )
        except SmtcUnavailable as exc:
            return {
                "ok": True,
                "applied": False,
                "reason": "smtc-unavailable",
                "note": str(exc),
            }
        if applied:
            return {"ok": True, "applied": True}
        return {"ok": True, "applied": False, "reason": "not-applied"}

    # ------------------------------------------------------------------ 取词
    def resolve_lyrics(self, track: Track) -> LyricsOutcome:
        """四级降级：本地 ``.lrc`` → 磁盘缓存 → 在线平台 → LRCLib。同一首歌只算一次。

        "只算一次"对 ``error`` 有个例外：那次失败可能只是握了个超时，隔一分钟重试。
        否则一次 SSL 超时会让这首歌在这次进程里**永远**没词——而 ``state()`` 每次都
        会调到这里。
        """

        now_ms = int(self._clock() * 1000)
        cached = self._lyrics_memory.get(track.id)
        if cached is not None:
            outcome, at_ms = cached
            # 命中的条件：好结果永远命中；``error`` 只在窗口**内**命中（窗口一过就要重问）。
            if outcome.status != "error" or now_ms - at_ms < LYRICS_ERROR_RETRY_MS:
                return outcome
        outcome = self._load_lyrics(track)
        self._lyrics_memory[track.id] = (outcome, now_ms)
        return outcome

    def _load_lyrics(self, track: Track) -> LyricsOutcome:
        local = load_local_lyrics(track, self._settings.music_lyrics_dir)
        if local is not None:
            return LyricsOutcome(status="ready", source="lrc-file", lines=local.lines)

        path = cache_path(self._settings.music_cache_dir, track)
        cached = read_cache(path)
        if cached is not None:
            return LyricsOutcome(status="ready", source=cached.source, lines=cached.lines)

        # 在线平台（生产装的是 QQ）放在 LRCLib 前面：中文歌在 QQ 上基本都有带时间轴的词，
        # 而 LRCLib 对中文歌既查得慢（真机上见过 ``_ssl.c:993`` 握手超时）又经常查不到。
        # **失败/查不到一律继续往下问**——在线这条线只是加分项，不该把 LRCLib 顶掉。
        if self._online_lyrics is not None:
            online = self._online_lyrics(track)
            if online.status == "ready":
                return online

        try:
            fetched = fetch_lrclib(track, client=self._lyrics_client)
        except LrclibUnavailable as exc:
            return LyricsOutcome(status="error", source="lrclib", note=f"LRCLib 取词失败：{exc}")
        if fetched is None:
            return LyricsOutcome(status="empty", source="lrclib")
        write_cache(path, fetched)
        return LyricsOutcome(status="ready", source=fetched.source, lines=fetched.lines)

    # ------------------------------------------------------------------ 本机曲库
    def library(self) -> dict[str, Any]:
        """``music_audio_dir`` 里的音频文件；关掉音乐时是空列表。"""

        if not self._settings.music_enabled:
            return {"items": []}
        directory = Path(self._settings.music_audio_dir)
        if not directory.is_dir():
            return {"items": []}
        items: list[dict[str, Any]] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
                continue
            items.append(
                {
                    "name": path.name,
                    "url": AUDIO_URL_PREFIX + urllib.parse.quote(path.name),
                    "sizeBytes": path.stat().st_size,
                }
            )
        return {"items": items}

    # ------------------------------------------------------------------ 内部
    def _hold_or_blank(
        self, now_ms: int, poll: dict[str, int], *, note: str = ""
    ) -> dict[str, Any]:
        """这一秒没读到 SMTC：窗口内把上一份原样顶上去，窗口外才说"没在放"。

        "原样"是字面意思——连 ``sampledAtMs`` 都不动，所以前端按"位置 + 流逝"算出来
        的进度条会照常往前走，界面连一格都不闪。宽限窗口过了再回空骨架，并把
        ``note``（SMTC 报了什么错）带上，免得"读不了"和"没在放"长一个样。
        """

        held = self._last
        if held is not None and now_ms - int(held["playback"]["sampledAtMs"]) <= HOLD_MS:
            return dict(held)  # 浅拷贝：调用方改不动我们这份
        self._last = None
        payload = self._blank("offline", now_ms, poll)
        if note:
            payload["note"] = note
        return payload

    def _poll(self) -> dict[str, int]:
        settings = self._settings
        return {
            "playing": max(MIN_POLL_MS, int(settings.music_poll_playing_ms)),
            "nearEnd": max(MIN_POLL_MS, int(settings.music_poll_near_end_ms)),
            "paused": max(MIN_POLL_MS, int(settings.music_poll_paused_ms)),
            "idle": max(MIN_POLL_MS, int(settings.music_poll_idle_ms)),
        }

    def _blank(self, connection: str, now_ms: int, poll: dict[str, int]) -> dict[str, Any]:
        """没有曲目时的骨架：字段一个不少，只是空着（前端不必写 ``?.`` 长链）。"""

        return {
            "ok": True,
            "connection": connection,
            "source": "smtc",
            "track": None,
            "playback": {
                "isPlaying": False,
                "positionMs": 0,
                "sampledAtMs": now_ms,
                "controls": [],
            },
            "lyrics": lyrics_json(LyricsOutcome(status="empty", source="none")),
            "pollMs": poll,
        }


def _track_json(track: Track) -> dict[str, Any]:
    """统一出口：字段名与参考项目一致（``durationMs`` / ``artworkUrl`` 是正名）。"""

    return {
        "id": track.id,
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "durationMs": track.duration_ms,
        "artworkUrl": track.artwork_url,
    }
