"""本机音乐播放 + 歌词视觉子系统（Web 控制台第 8 栏的后端）。"""

from yixiang.media.lrclib import (
    LrclibUnavailable,
    cache_path,
    fetch_lrclib,
    read_cache,
    write_cache,
)
from yixiang.media.lyrics import LyricLine, Lyrics, Track, load_local_lyrics, parse_lrc
from yixiang.media.service import (
    ACTIONS,
    AUDIO_SUFFIXES,
    AUDIO_URL_PREFIX,
    MIN_POLL_MS,
    LyricsOutcome,
    MusicService,
    lyrics_json,
    track_from_query,
)
from yixiang.media.smtc import SmtcSnapshot, SmtcUnavailable, control, read_current

__all__ = [
    "ACTIONS",
    "AUDIO_SUFFIXES",
    "AUDIO_URL_PREFIX",
    "MIN_POLL_MS",
    "LrclibUnavailable",
    "LyricLine",
    "Lyrics",
    "LyricsOutcome",
    "MusicService",
    "SmtcSnapshot",
    "SmtcUnavailable",
    "Track",
    "cache_path",
    "control",
    "fetch_lrclib",
    "load_local_lyrics",
    "lyrics_json",
    "parse_lrc",
    "read_cache",
    "read_current",
    "track_from_query",
    "write_cache",
]
