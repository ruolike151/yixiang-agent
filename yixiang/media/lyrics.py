"""歌词领域模型与本地 ``.lrc`` 解析。

统一形状与参考项目一致：毫秒时间戳 + 文本（``{"timeMs": 0, "text": "..."}``）。
``LyricLine.words`` 是**逐字**的预留位（在线 YRC/KRC/QRC 会填它），
本计划只渲染逐句，所以默认是空元组。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# [00:12.34] / [00:12.345] / [00:12] / [1:02:03.45]
_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_OFFSET = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)
_METADATA = re.compile(r"^\[(ar|ti|al|by|re|ve|length):", re.IGNORECASE)


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    artwork_url: str = ""


@dataclass(frozen=True)
class LyricLine:
    time_ms: int
    text: str
    words: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class Lyrics:
    lines: tuple[LyricLine, ...]
    source: str
    word_by_word: bool = False

    def is_empty(self) -> bool:
        return not self.lines


def _stamp_ms(minutes: str, seconds: str, fraction: str | None) -> int:
    scale = {1: 100, 2: 10, 3: 1}.get(len(fraction or ""), 1)
    millis = int(fraction or 0) * scale
    return (int(minutes) * 60 + int(seconds)) * 1000 + millis


def parse_lrc(text: str) -> tuple[LyricLine, ...]:
    """``[mm:ss.xx]`` 文本 → 按时间排序的 ``LyricLine`` 元组。

    规则（都有用例钉着）：同一行多个时间戳展开成多行；``[offset:±ms]`` 平移所有行；
    ``[ar:]`` / ``[ti:]`` 这类元数据与空文本行丢掉；没有时间戳的行丢掉。
    """

    offset_ms = 0
    for raw in text.splitlines():
        match = _OFFSET.search(raw)
        if match:
            offset_ms = int(match.group(1))
            break

    collected: list[LyricLine] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _METADATA.match(line):
            continue
        stamps = list(_STAMP.finditer(line))
        if not stamps:
            continue
        content = line[stamps[-1].end() :].strip()
        if not content:
            continue
        for stamp in stamps:
            time_ms = _stamp_ms(*stamp.groups()) + offset_ms
            collected.append(LyricLine(time_ms=max(0, time_ms), text=content))
    collected.sort(key=lambda item: item.time_ms)
    return tuple(collected)


def _norm(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())


def find_local_lyrics(track: Track, lyrics_dir: Path) -> Path | None:
    """在 ``lyrics_dir`` 里找 ``*.lrc``：先精确匹配归一化文件名，再退到"文件名包含标题"。"""

    directory = Path(lyrics_dir)
    if not directory.is_dir():
        return None
    files = sorted(directory.glob("*.lrc"))
    if not files:
        return None

    candidates = [f"{track.artist} - {track.title}", track.title, Path(track.id).stem]
    wanted = [key for key in (_norm(item) for item in candidates) if key]
    for key in wanted:
        for path in files:
            if _norm(path.stem) == key:
                return path
    title = _norm(track.title)
    if title:
        for path in sorted(files, key=lambda item: len(item.stem)):
            if title in _norm(path.stem):
                return path
    return None


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def load_local_lyrics(track: Track, lyrics_dir: Path) -> Lyrics | None:
    path = find_local_lyrics(track, lyrics_dir)
    if path is None:
        return None
    lines = parse_lrc(_read_text(path))
    if not lines:
        return None
    return Lyrics(lines=lines, source="lrc-file")
