"""网易云音乐适配器（Task 7）。

``base`` 指**本机自建的 NeteaseCloudMusicApi**（典型值是 ``http://127.0.0.1:3000``），
不是网易云的官方域名——参考项目也是这么接的（``VITE_NETEASE_API_BASE`` 指向同一个服务）。
四个接口：

  * ``/cloudsearch`` 搜歌（``result.songs`` / ``result.songCount``）；
  * ``/song/detail?ids=`` 取详情（``songs[0]``）；
  * ``/song/url/v1?id=&level=exhigh`` 取直链（``data[0].url``，``null`` = 放不了）；
  * ``/lyric/new?id=`` 取词（``lrc.lyric``；``yrc`` 非空就在数据层记一笔"有逐字"）。

``randomCNIP=true`` / ``https=true`` 与参考项目同款：前者绕开海外 IP 的版权墙，
后者保证拿回来的是 ``https://`` 直链（浏览器在 ``http://127.0.0.1`` 下允许加载它）。
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


def _song_from_node(raw: Any) -> Song:
    """网易云的"歌"只有一个形状：``/cloudsearch`` 与 ``/song/detail`` 共用它。"""

    node = as_dict(raw)
    album = as_dict(node.get("al"))
    return Song(
        provider="netease",
        id=str(pick(node, "id") or ""),
        title=str(pick(node, "name") or ""),
        artist=join_artists(node.get("ar"), "name"),
        album=str(pick(album, "name") or ""),
        duration_ms=to_int(pick(node, "dt")),
        artwork_url=str(pick(album, "picUrl") or ""),
    )


class NeteaseProvider(HttpProvider):
    name = "netease"
    display_name = "网易云音乐"

    def search_songs(
        self, keyword: str, *, limit: int = 20, offset: int = 0
    ) -> ProviderPage:
        size = max(1, min(int(limit), 100))
        start = max(0, int(offset))
        payload = self.get_json(
            "/cloudsearch",
            params={"keywords": keyword, "limit": size, "offset": start},
        )
        result = as_dict(as_dict(payload).get("result"))
        items = tuple(_song_from_node(raw) for raw in as_list(result.get("songs")))
        total = to_int(result.get("songCount"), default=start + len(items))
        next_offset = start + len(items)
        return ProviderPage(
            items=items,
            has_more=next_offset < total,
            next_offset=next_offset,
            total=total,
        )

    def get_song_detail(self, song_id: str) -> Song:
        payload = self.get_json("/song/detail", params={"ids": song_id})
        songs = as_list(as_dict(payload).get("songs"))
        if not songs:
            raise ProviderError(
                "invalid-response", f"网易云没给这首歌的详情：{song_id}", self.name
            )
        return _song_from_node(songs[0])

    def get_audio_source(self, song: Song) -> str:
        payload = self.get_json(
            "/song/url/v1",
            params={
                "id": song.id,
                "level": "exhigh",
                "randomCNIP": "true",
                "https": "true",
            },
        )
        entries = as_list(as_dict(payload).get("data"))
        url = pick(as_dict(entries[0]), "url") if entries else None
        if not url:
            raise ProviderError(
                "not-playable",
                f"网易云不给这首的直链（会员 / 版权 / 地区）：{song.title or song.id}",
                self.name,
            )
        return str(url)

    def get_lyrics(self, song: Song) -> ProviderLyrics | None:
        payload = as_dict(self.get_json("/lyric/new", params={"id": song.id}))
        lines = parse_lrc(str(pick(as_dict(payload.get("lrc")), "lyric") or ""))
        if not lines:
            return None
        # yrc 是逐字；本计划只"记一笔"，逐字渲染留给以后（见取舍 4）。
        wordy = bool(pick(as_dict(payload.get("yrc")), "lyric"))
        return ProviderLyrics(lines=lines, word_by_word=wordy)
