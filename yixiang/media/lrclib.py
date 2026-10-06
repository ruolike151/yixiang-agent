"""LRCLib 兜底取词 + 磁盘缓存。

LRCLib（https://lrclib.net）是公开歌词库，不要密钥。本模块只做三件事：
查一次、缓存下来、下次先读缓存。**不做重试**——网络抖动留给上层"下一轮轮询再试"，
在这里 sleep 重试会把 Web 请求线程拖住。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx

from yixiang.media.lyrics import LyricLine, Lyrics, Track, parse_lrc

LRCLIB_ENDPOINT = "https://lrclib.net/api/get"
DEFAULT_TIMEOUT = 8.0


class LrclibUnavailable(RuntimeError):
    """LRCLib 暂时用不了（网络 / 超时 / 5xx），与"查不到"分开。

    查不到返回 ``None``；用不了抛这个，让调用方决定"这次没歌词"还是"下次再试"。
    """


def cache_path(cache_dir: Path, track: Track) -> Path:
    """缓存文件名 = 歌名 / 歌手 / 专辑 / 时长的 sha1。四个字段任一不同就是另一首歌。"""

    key = "|".join((track.title, track.artist, track.album, str(track.duration_ms)))
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return Path(cache_dir) / f"{digest}.json"


def write_cache(path: Path, lyrics: Lyrics) -> None:
    """原子写：先写 ``.json.tmp`` 再 ``replace``，半截文件永远不会被读到。"""

    payload = {
        "source": lyrics.source,
        "wordByWord": lyrics.word_by_word,
        "lines": [
            {"timeMs": line.time_ms, "text": line.text, "words": list(line.words)}
            for line in lyrics.lines
        ],
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def read_cache(path: Path) -> Lyrics | None:
    """读不出、解析不了、一行都没有 —— 一律 ``None``（当缓存不存在，别把坏文件当歌词）。"""

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    items = raw.get("lines")
    if not isinstance(items, list):
        return None
    lines: list[LyricLine] = []
    for item in items:
        if not isinstance(item, dict) or "timeMs" not in item or "text" not in item:
            continue
        words = item.get("words")
        pairs = tuple(
            (int(pair[0]), str(pair[1]))
            for pair in (words or [])
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        )
        lines.append(LyricLine(time_ms=int(item["timeMs"]), text=str(item["text"]), words=pairs))
    if not lines:
        return None
    return Lyrics(
        lines=tuple(lines),
        source=str(raw.get("source") or "lrclib"),
        word_by_word=bool(raw.get("wordByWord")),
    )


def fetch_lrclib(
    track: Track,
    *,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Lyrics | None:
    """按歌名 / 歌手 / 专辑 / 时长精确查一次；只认带时间轴的 ``syncedLyrics``。"""

    params: dict[str, str] = {"track_name": track.title}
    if track.artist:
        params["artist_name"] = track.artist
    if track.album:
        params["album_name"] = track.album
    if track.duration_ms > 0:
        # 单位是**秒**（整数）：传毫秒会 404，而且表现和"查不到"一样
        params["duration"] = str(round(track.duration_ms / 1000))

    owned = client is None
    http = client if client is not None else httpx.Client(timeout=timeout)
    try:
        response = http.get(LRCLIB_ENDPOINT, params=params)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise LrclibUnavailable(f"LRCLib 返回 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LrclibUnavailable("LRCLib 返回的不是 JSON") from exc
        synced = payload.get("syncedLyrics") if isinstance(payload, dict) else None
        if not synced:
            return None  # 只有 plainLyrics：逐句高亮用不了，等于没歌词
        lines = parse_lrc(str(synced))
        if not lines:
            return None
    except httpx.HTTPError as exc:
        raise LrclibUnavailable(f"LRCLib 请求失败：{exc}") from exc
    finally:
        if owned:
            http.close()
    return Lyrics(lines=lines, source="lrclib")
