"""本机音乐播放 + 歌词视觉子系统（Web 控制台第 8 栏的后端）。"""

from yixiang.media.lrclib import (
    LrclibUnavailable,
    cache_path,
    fetch_lrclib,
    read_cache,
    write_cache,
)
from yixiang.media.lyrics import LyricLine, Lyrics, Track, load_local_lyrics, parse_lrc

__all__ = [
    "LrclibUnavailable",
    "LyricLine",
    "Lyrics",
    "Track",
    "cache_path",
    "fetch_lrclib",
    "load_local_lyrics",
    "parse_lrc",
    "read_cache",
    "write_cache",
]
