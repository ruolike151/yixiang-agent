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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._lyrics_client = lyrics_client
        self._clock = clock
        # 按 track.id 存"这首歌问过什么"：ready / empty / error 一律缓存。
        # 切歌换 id 自然失效；磁盘缓存（Task 2）只管已查到的好结果。
        self._lyrics_memory: dict[str, LyricsOutcome] = {}

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
            payload = self._blank("offline", now_ms, poll)
            payload["note"] = f"SMTC 读取失败：{exc}"
            return payload
        if snapshot is None:
            return self._blank("offline", now_ms, poll)

        position = snapshot.position_ms
        if snapshot.duration_ms > 0:
            position = min(position, snapshot.duration_ms)
        return {
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
        """三级降级：本地 ``.lrc`` → 磁盘缓存 → LRCLib。同一首歌只算一次。"""

        cached = self._lyrics_memory.get(track.id)
        if cached is not None:
            return cached
        outcome = self._load_lyrics(track)
        self._lyrics_memory[track.id] = outcome
        return outcome

    def _load_lyrics(self, track: Track) -> LyricsOutcome:
        local = load_local_lyrics(track, self._settings.music_lyrics_dir)
        if local is not None:
            return LyricsOutcome(status="ready", source="lrc-file", lines=local.lines)

        path = cache_path(self._settings.music_cache_dir, track)
        cached = read_cache(path)
        if cached is not None:
            return LyricsOutcome(status="ready", source=cached.source, lines=cached.lines)

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
