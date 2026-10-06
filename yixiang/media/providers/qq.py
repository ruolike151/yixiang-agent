"""QQ 音乐适配器（Task 7）。

详情与直链走本机自建的 ``VITE_QQ_API_BASE``（典型值 ``http://127.0.0.1:3300``）；
**搜索与取词不在这里**——它们没有自建服务能替，参考项目也是浏览器直连，所以自己走公网：

  * 搜索：``GET https://c.y.qq.com/soso/fcgi-bin/client_search_cp``。原来是 musicu 的
    ``DoSearchForQQMusicLite``，那个入口**已经废了**：HTTP 200、``code=0``，但
    ``request.code=2001``、``item_song`` 是空数组（面板表现就是"怎么搜都搜不出来"）。
    soso 这个老入口还活着，字段名（``mid`` / ``name`` / ``singer`` / ``album`` /
    ``interval``）与 musicu 那份一模一样，所以 ``_song_from_row`` 一行都不用改。
  * 取词：``POST https://u.y.qq.com/cgi-bin/musicu.fcg`` 的
    ``music.musichallSong.PlayLyricInfo``。**同一个入口要两次能拿到两级词**：
    ``qrc=1`` 的 ``lyric`` 是 QRC 密文（逐字，``providers/qrc.py`` 解），
    ``qrc=0`` 的同一个字段是 base64 的普通 LRC（逐句，**根本不用解密**）。
    所以"QRC 解不开"不是终点：再要一次 ``qrc=0``，逐句照样有词（Folia 退到逐句渲染）。

``lyrics_for_track()`` 是给"系统正在播放"那条链路用的：SMTC 只给歌名 / 歌手（没有
QQ 的 songmid），所以先搜一次拿 mid、按歌名 + 歌手 + 时长挑候选，再走 :meth:`get_lyrics`。
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

from yixiang.media.lyrics import Track, parse_lrc
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

# 取词那条路：参考项目 utils/lyrics/providers/qqLyricProvider.ts 同一条路，浏览器直连 musicu。
MUSICU_SEARCH_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
# 匿名会话：``tmeLoginType=-1`` + okhttp UA 是参考项目用的那套，不落任何账号凭据。
SEARCH_HEADERS = {
    "Content-Type": "application/json",
    "Cookie": "tmeLoginType=-1;",
    "User-Agent": "okhttp/3.14.9",
}

# 搜索那条路：musicu 的 Lite 入口已废（``request.code=2001``），soso 这个 GET 还活着。
# 它认的是浏览器 UA + ``Referer: https://y.qq.com/``（匿名，不带任何账号凭据）。
LEGACY_SEARCH_URL = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
LEGACY_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://y.qq.com/",
}

# 同一条 musicu 入口的歌词方法：``qrc: 1`` 时 ``lyric`` 是 QRC 密文，否则是 base64 LRC。
LYRIC_MODULE = "music.musichallSong.PlayLyricInfo"
LYRIC_METHOD = "GetPlayLyricInfo"

# "系统正在播放"没有 songmid，只能靠搜一次再认。搜太宽会认错、认错比没词更糟，
# 所以只取前几条按下面的三条规则卡（歌名 / 歌手 / 时长），宁可不认也不认错。
MATCH_LIMIT = 10
# 时长的容忍度：现场版 / 伴奏 / 串烧与录音室的差通常远大于这个数，卡住正好把它们挡掉。
MATCH_DURATION_SLACK_MS = 8_000


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


def _key(value: str) -> str:
    """归一化：小写、去掉空格 / 标点 / 括号之类的装饰（``晴天 (Live)`` → ``晴天live``）。"""

    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (value or "").lower())


def _names(value: str) -> set[str]:
    """``"周杰伦/费玉清"`` 这种多歌手串 → 归一化后的名字集合。"""

    parts = re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", value or "")
    return {_key(part) for part in parts if _key(part)}


def _same_song(want: Track, got: Song) -> bool:
    """歌名 + 歌手 + 时长三条同时对上才算同一首（某一条是空的就不拿它卡人）。

    歌名用"互相包含"而不是相等：SMTC 给的常常是 ``晴天 (Live)`` / ``晴天 - Live``，
    去掉装饰之后两边都归成 ``晴天live``，正好对上。
    """

    mine, theirs = _key(want.title), _key(got.title)
    if not mine or not theirs or not (mine == theirs or mine in theirs or theirs in mine):
        return False
    if want.artist and got.artist and not (_names(want.artist) & _names(got.artist)):
        return False
    if want.duration_ms > 0 and got.duration_ms > 0:
        return abs(want.duration_ms - got.duration_ms) <= MATCH_DURATION_SLACK_MS
    return True


def _best_match(items: tuple[Song, ...], track: Track) -> Song | None:
    """搜索结果按相关度排，取**第一个**对得上的；一条都对不上就 ``None``。"""

    for song in items:
        if _same_song(track, song):
            return song
    return None


class QqProvider(HttpProvider):
    name = "qq"
    display_name = "QQ 音乐"

    def search_songs(
        self, keyword: str, *, limit: int = 20, offset: int = 0
    ) -> ProviderPage:
        size = max(1, min(int(limit), 100))
        start = max(0, int(offset))
        payload = self._fetch(
            "GET",
            LEGACY_SEARCH_URL,
            params={
                "w": keyword,
                "format": "json",
                "p": start // size + 1,
                "n": size,
                "cr": 1,
                "new_json": 1,
                "t": 0,
                "aggr": 1,
            },
            headers=LEGACY_SEARCH_HEADERS,
        )
        root = as_dict(payload)
        if to_int(root.get("code"), default=-1) != 0:
            raise ProviderError(
                "invalid-response", "QQ 音乐搜索没通过（code 不是 0）。", self.name
            )
        data = as_dict(as_dict(root.get("data")).get("song"))
        items = tuple(_song_from_row(raw) for raw in as_list(data.get("list")))
        total = to_int(pick(data, "totalnum", "total"), default=start + len(items))
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
        """QQ 的歌：**逐字优先**（QRC），解不开就退成逐句（``qrc=0`` 的普通 LRC）。

        ``qrc=1`` 拿到的 ``lyric`` 是 QRC 密文——解不开是常态（上游换过算法），但那**不是**
        "这首歌没词"：同一个字段在 ``qrc=0`` 里是 base64 的明文 LRC。所以这里多要一次，
        逐句照样有词，歌词舞台退到逐句渲染。
        """

        word_level = self._word_level_lyrics(song.id)
        return word_level if word_level is not None else self._line_level_lyrics(song.id)

    def lyrics_for_track(self, track: Track) -> ProviderLyrics | None:
        """"系统正在播放"那条链路：只有歌名 / 歌手，先搜一次拿 mid 再取词。

        **任何取不到的原因都回 ``None``**（搜不到、对不上、网络炸）：调用方
        （``MusicService``）接着会去问 LRCLib，不能让 QQ 的一次失败把别的来源一起带走。
        """

        keyword = " ".join(
            part.strip() for part in (track.title, track.artist) if part.strip()
        )
        if not keyword:
            return None
        try:
            page = self.search_songs(keyword, limit=MATCH_LIMIT)
        except ProviderError:
            return None
        best = _best_match(page.items, track)
        if best is None:
            return None
        try:
            return self.get_lyrics(best)
        except ProviderError:
            return None

    # ---- 内部：两级词
    def _word_level_lyrics(self, song_id: str) -> ProviderLyrics | None:
        """QRC 逐字；解不开 / 这首歌没有 QRC 就回 ``None``（交给逐句那条线）。"""

        data = self._lyric_data(song_id, qrc=1)
        blob = str(pick(data, "lyric") or "")
        if to_int(data.get("qrc")) != 1 or not blob:
            return None
        try:
            plain = qrc_decrypt(blob)
        except QrcError:
            return None
        lines, karaoke = parse_qrc(plain)
        if not lines:
            return None
        return ProviderLyrics(lines=lines, word_by_word=True, karaoke_lyric=karaoke)

    def _line_level_lyrics(self, song_id: str) -> ProviderLyrics:
        """``qrc=0`` 的逐句 LRC（base64 明文，不涉及解密）。没词就是空词，不抛。"""

        data = self._lyric_data(song_id, qrc=0)
        blob = str(pick(data, "lyric") or "")
        if not blob:
            return ProviderLyrics(note="QQ 没给这首歌的词。")
        try:
            text = base64.b64decode(blob).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return ProviderLyrics(note="QQ 给的歌词不是 base64，认不出。")
        lines = parse_lrc(text)
        return ProviderLyrics(lines=lines, note="" if lines else "QQ 只给了空 LRC。")

    def _lyric_data(self, song_id: str, *, qrc: int) -> dict[str, Any]:
        """走一次 ``GetPlayLyricInfo``，只取 ``req_1.data`` 那一层。"""

        payload = self.post_json(
            MUSICU_SEARCH_URL,
            {
                "req_1": {
                    "module": LYRIC_MODULE,
                    "method": LYRIC_METHOD,
                    "param": {"format": "json", "songMID": song_id, "qrc": qrc},
                }
            },
            headers=SEARCH_HEADERS,
        )
        return as_dict(as_dict(as_dict(payload).get("req_1")).get("data"))
