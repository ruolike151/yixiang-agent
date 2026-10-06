"""QQ 音乐适配器（Task 7）。

详情与直链走本机自建的 ``VITE_QQ_API_BASE``（典型值 ``http://127.0.0.1:3300``）；
**搜索不在这里**——QQ 的 Lite 搜索（``DoSearchForQQMusicLite``）没有自建服务能替，
参考项目也是浏览器直连，所以这条路径照抄：``POST https://u.y.qq.com/cgi-bin/musicu.fcg``。

歌词走 **QRC 逐字**（取舍 5）：还是这条 musicu 入口，换成
``music.musichallSong.PlayLyricInfo``；拿回来的 ``lyric`` 是 base64 密文，
``providers/qrc.py`` 用 3DES-ECB + raw inflate 解开，整串原文落进 ``karaoke_lyric``
（Folia 引擎 task-10 直接吃它）。解不开就老实降级成空词 + ``note``，不假装有词。
"""

from __future__ import annotations

import base64
import time
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
from yixiang.media.providers.qrc import QrcError, parse_qrc, qrc_decrypt

# 参考项目 utils/lyrics/providers/qqLyricProvider.ts 同一条路：浏览器直连 musicu。
MUSICU_SEARCH_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
# 匿名会话：``tmeLoginType=-1`` + okhttp UA 是参考项目用的那套，不落任何账号凭据。
SEARCH_HEADERS = {
    "Content-Type": "application/json",
    "Cookie": "tmeLoginType=-1;",
    "User-Agent": "okhttp/3.14.9",
}

# 同一条 musicu 入口的歌词方法：``qrc: 1`` 时 ``lyric`` 是 QRC 密文，否则是 base64 LRC。
LYRIC_MODULE = "music.musichallSong.PlayLyricInfo"
LYRIC_METHOD = "GetPlayLyricInfo"


def _song_from_row(raw: Any) -> Song:
    node = as_dict(raw)
    album = as_dict(node.get("album"))
    album_mid = str(pick(album, "mid") or "")
    artwork = (
        f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg"
        if album_mid
        else ""
    )
    extra: dict[str, str] = {}
    songid = pick(node, "id")
    if songid is not None:
        extra["songid"] = str(songid)
    if album_mid:
        extra["album_mid"] = album_mid
    return Song(
        provider="qq",
        id=str(pick(node, "mid", "songmid") or ""),
        title=str(pick(node, "title", "name", "songname") or ""),
        artist=join_artists(node.get("singer"), "name"),
        album=str(pick(album, "name", "title") or ""),
        duration_ms=to_int(pick(node, "interval")) * 1000,
        artwork_url=artwork,
        extra=extra,
    )


class QqProvider(HttpProvider):
    name = "qq"
    display_name = "QQ 音乐"

    def search_songs(
        self, keyword: str, *, limit: int = 20, offset: int = 0
    ) -> ProviderPage:
        size = max(1, min(int(limit), 100))
        start = max(0, int(offset))
        body = {
            "comm": {
                "ct": 11,
                "cv": "1003006",
                "v": "1003006",
                "os_ver": "15",
                "phonetype": "24122RKC7C",
                "rom": "MIUI_13",
                "tmeAppID": "qqmusiclight",
                "nettype": "NETWORK_WIFI",
                "udid": "0",
                "uid": "0",
            },
            "request": {
                "method": "DoSearchForQQMusicLite",
                "module": "music.search.SearchCgiService",
                "param": {
                    "search_id": str(int(time.time() * 1000)),
                    "remoteplace": "search.android.keyboard",
                    "query": keyword,
                    "search_type": 0,
                    "num_per_page": size,
                    "page_num": start // size + 1,
                    "highlight": 0,
                    "nqc_flag": 0,
                    "page_id": 1,
                    "grp": 1,
                },
            },
        }
        payload = self.post_json(MUSICU_SEARCH_URL, body, headers=SEARCH_HEADERS)
        root = as_dict(payload)
        request_node = as_dict(root.get("request"))
        if (
            to_int(root.get("code"), default=-1) != 0
            or to_int(request_node.get("code"), default=-1) != 0
        ):
            raise ProviderError(
                "invalid-response", "QQ 音乐搜索没通过（code 不是 0）。", self.name
            )
        data = as_dict(as_dict(request_node.get("data")).get("body"))
        items = tuple(
            _song_from_row(raw) for raw in as_list(pick(data, "item_song", "song"))
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
        payload = self.get_json(f"/getSongInfo/{song_id}")
        response = as_dict(as_dict(payload).get("response"))
        songinfo = as_dict(response.get("songinfo"))
        track = as_dict(as_dict(songinfo.get("data")).get("track_info"))
        if not track:
            raise ProviderError(
                "invalid-response", f"QQ 音乐没给这首歌的详情：{song_id}", self.name
            )
        return _song_from_row(track)

    def get_audio_source(self, song: Song) -> str:
        """先要 320，拿不到就退到 128（参考项目同款退化）。"""

        for quality in ("320", "128"):
            params: dict[str, Any] = {"quality": quality}
            media_id = song.extra.get("songid")
            if media_id:
                params["mediaId"] = media_id
            payload = self.get_json(f"/getMusicPlay/{song.id}", params=params)
            data = as_dict(payload).get("data")
            entry = as_dict(as_dict(as_dict(data).get("playUrl")).get(song.id))
            url = str(pick(entry, "url", "purl") or "")
            if url:
                return url
        raise ProviderError(
            "not-playable",
            f"QQ 音乐不给这首的直链（会员 / 版权）：{song.title or song.id}",
            self.name,
        )

    def get_lyrics(self, song: Song) -> ProviderLyrics | None:
        """QQ 的歌：**能走 QRC 就走 QRC**（逐字），否则退 base64 LRC（逐句）。"""

        payload = self.post_json(
            MUSICU_SEARCH_URL,
            {
                "req_1": {
                    "module": LYRIC_MODULE,
                    "method": LYRIC_METHOD,
                    "param": {"format": "json", "songMID": song.id, "qrc": 1},
                }
            },
            headers=SEARCH_HEADERS,
        )
        data = as_dict(as_dict(as_dict(payload).get("req_1")).get("data"))
        blob = str(pick(data, "lyric") or "")
        if not blob:
            return None
        if to_int(data.get("qrc")) == 1:
            try:
                plain = qrc_decrypt(blob)
            except QrcError as exc:
                # 密文坏了 / 上游换算法：降级成空词 + 说明，不假装有词。
                return ProviderLyrics(note=f"QQ 的 QRC 解不开（{exc}）——先只报空。")
            lines, karaoke = parse_qrc(plain)
            if not lines:
                return ProviderLyrics(note="QQ 的 QRC 解开了，但里面没有行。")
            return ProviderLyrics(lines=lines, word_by_word=True, karaoke_lyric=karaoke)
        # 没 QRC 的歌（纯音乐 / 版权）：``lyric`` 是 base64 的 LRC，退回逐句。
        text = base64.b64decode(blob).decode("utf-8", "replace")
        lines = parse_lrc(text)
        return ProviderLyrics(lines=lines, note="" if lines else "QQ 只给了空 LRC。")
