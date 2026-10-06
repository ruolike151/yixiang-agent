"""酷狗音乐适配器（Task 7）。

``base`` 指本机自建的酷狗 API（参考项目用 ``VITE_KUGOU_API_BASE``）。这条链上最容易
踩的两个坑都在这儿收口：

  * **hash 一律大写。** 酷狗搜索回的是小写，取直链 / 取词只认大写——同一首歌的大小写
    对不上就是"搜得到、放不了"。
  * **``Duration`` 可能是秒也可能是毫秒。** 小于 10000 当秒（``240 → 240000``），
    否则原样当毫秒。

歌词两步走：``/search/lyric`` 拿 ``candidates[0]`` 的 ``{id, accesskey}``，再打 ``/lyric``。
本计划用 ``fmt=lrc`` + ``decode=true``（后端解成明文逐句），不用 ``fmt=krc``——见取舍 4。
"""

from __future__ import annotations

from typing import Any

from yixiang.media.lyrics import parse_lrc
from yixiang.media.providers.base import (
    HttpProvider,
    ProviderError,
    ProviderLyrics,
    ProviderPage,
    Song,
    as_dict,
    as_list,
    join_artists,
    pick,
    to_int,
)


def _first_url(data: Any) -> str:
    """直链可能在 ``data.url`` 也可能在 ``data[0].url``，两种都收。"""

    if isinstance(data, dict):
        return str(pick(data, "url", "play_url", "playUrl") or "")
    for entry in as_list(data):
        url = pick(as_dict(entry), "url", "play_url", "playUrl")
        if url:
            return str(url)
    return ""


def _song_from_row(raw: Any) -> Song:
    node = as_dict(raw)
    album = as_dict(node.get("album_info")) or as_dict(node.get("album"))
    duration = to_int(pick(node, "Duration", "duration", "timelength"))
    if 0 < duration < 10_000:
        duration *= 1000
    artwork = str(pick(node, "Image", "img", "image") or "")
    if artwork and not artwork.startswith("http"):
        artwork = f"https://c1.kgimg.com/stdmusic/400/{artwork}"
    extra: dict[str, str] = {}
    album_id = pick(node, "album_id", "AlbumID", "albumId")
    if album_id is not None:
        extra["album_id"] = str(album_id)
    audio_id = pick(node, "album_audio_id", "albumAudioId", "audio_id")
    if audio_id is not None:
        extra["album_audio_id"] = str(audio_id)
    return Song(
        provider="kugou",
        id=str(pick(node, "FileHash", "fileHash", "hash", "Hash") or "").upper(),
        title=str(pick(node, "SongName", "songname", "name") or ""),
        artist=join_artists(pick(node, "SingerName", "singers"), "name", "singer_name"),
        album=str(
            pick(node, "AlbumName", "album_name")
            or pick(album, "album_name", "name")
            or ""
        ),
        duration_ms=max(0, duration),
        artwork_url=artwork,
        extra=extra,
    )


class KugouProvider(HttpProvider):
    name = "kugou"
    display_name = "酷狗音乐"

    def search_songs(
        self, keyword: str, *, limit: int = 20, offset: int = 0
    ) -> ProviderPage:
        size = max(1, min(int(limit), 100))
        start = max(0, int(offset))
        payload = self.get_json(
            "/search",
            params={
                "keywords": keyword,
                "keyword": keyword,
                "type": "song",
                "page": start // size + 1,
                "pagesize": size,
            },
        )
        data = as_dict(as_dict(payload).get("data"))
        items = tuple(
            _song_from_row(raw)
            for raw in as_list(pick(data, "lists", "songs", "info"))
        )
        total = to_int(pick(data, "total"), default=start + len(items))
        next_offset = start + len(items)
        return ProviderPage(
            items=items,
            has_more=next_offset < total,
            next_offset=next_offset,
            total=total,
        )

    def get_song_detail(self, song_id: str) -> Song:
        payload = self.get_json("/audio", params={"hash": song_id.upper()})
        data = as_dict(as_dict(payload).get("data"))
        rows = as_list(pick(data, "lists", "info"))
        node = as_dict(rows[0]) if rows else data
        if not node:
            raise ProviderError(
                "invalid-response", f"酷狗没给这首歌的详情：{song_id}", self.name
            )
        return _song_from_row(node)

    def get_audio_source(self, song: Song) -> str:
        params: dict[str, Any] = {"hash": song.id.upper(), "quality": "320"}
        for key in ("album_id", "album_audio_id"):
            value = song.extra.get(key)
            if value:
                params[key] = value
        url = _first_url(as_dict(self.get_json("/song/url", params=params)).get("data"))
        if not url:
            raise ProviderError(
                "not-playable",
                f"酷狗不给这首的直链（版权 / 付费）：{song.title or song.id}",
                self.name,
            )
        return url

    def get_lyrics(self, song: Song) -> ProviderLyrics | None:
        params: dict[str, Any] = {
            "hash": song.id.upper(),
            "duration": int(song.duration_ms) or 0,
            "man": "no",
        }
        audio_id = song.extra.get("album_audio_id")
        if audio_id:
            params["album_audio_id"] = audio_id
        candidates = as_list(
            pick(self.get_json("/search/lyric", params=params), "candidates")
        )
        if not candidates:
            return None
        candidate = as_dict(candidates[0])
        lyric_id = pick(candidate, "id")
        accesskey = pick(candidate, "accesskey")
        if lyric_id is None or not accesskey:
            return None
        payload = self.get_json(
            "/lyric",
            params={
                "id": lyric_id,
                "accesskey": accesskey,
                "fmt": "lrc",
                "decode": "true",
            },
        )
        text = str(pick(as_dict(payload).get("data"), "decodeContent", "content") or "")
        lines = parse_lrc(text)
        if not lines:
            return None
        return ProviderLyrics(lines=lines)
