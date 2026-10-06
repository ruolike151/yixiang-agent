"""在线平台 provider 层：三家适配器 + registry + 两条 HTTP 路由（Task 7）。

三家后端一律用 ``httpx.MockTransport`` 假装，**一条都不出网**：

  * 适配器层只测"这家 JSON → ``Song`` / ``ProviderLyrics``"的翻译，以及错误映射；
  * 路由层起一台真 ``ConsoleServer``，测前端真正会打的那两个 URL 与状态码。

QQ 的搜索打在 ``c.y.qq.com``、取词打在 ``u.y.qq.com`` 上（都是浏览器直连那条路），
所以假 transport 是**唯一**能让这组用例离线跑起来的东西。真机对照见任务文档 Step 5。
"""

from __future__ import annotations

import base64
import contextlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import make_settings
from music_fakes import playing, session_factory

from yixiang.media.lyrics import LyricLine, Track
from yixiang.media.providers.base import ProviderError, ProviderLyrics, Song
from yixiang.media.providers.kugou import KugouProvider
from yixiang.media.providers.netease import NeteaseProvider
from yixiang.media.providers.qq import QqProvider
from yixiang.media.providers.qrc import qrc_encrypt
from yixiang.media.providers.registry import build_providers, get_provider
from yixiang.media.service import MusicService
from yixiang.web.console import ConsoleAPI, build_music_service
from yixiang.web.server import ConsoleServer

NETEASE = "http://netease.test"
KUGOU = "http://kugou.test"
QQ = "http://qq.test"


# --------------------------------------------------------------------- 假后端
def _client(routes: dict[str, Any], *, status: int = 200) -> httpx.Client:
    """按"路径结尾"分派：``/cloudsearch`` / ``/getMusicPlay/xx`` 都能对上。

    键的顺序就是匹配顺序，所以更长的路径要写在前面（``/search/lyric`` 先于 ``/lyric``）。
    ``routes`` 里放 ``httpx.Response`` 或可调用对象（可调用对象自己回 Response）。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, payload in routes.items():
            if not request.url.path.endswith(suffix):
                continue
            if callable(payload):
                return payload(request)
            return httpx.Response(status, json=payload)
        return httpx.Response(404, json={"message": f"没有这个接口：{request.url.path}"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _api(settings: Any, *, providers: Any = None) -> ConsoleAPI:
    """一个控制台：音乐那一半用假 SMTC + 假 LRCLib，provider 那一半用假后端。"""

    def no_lyrics(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "no"})

    service = MusicService(
        settings,
        session_factory=session_factory(playing()),
        lyrics_client=httpx.Client(transport=httpx.MockTransport(no_lyrics)),
        clock=lambda: 1_786_000_000.0,
    )
    kwargs: dict[str, Any] = {"music_factory": lambda current: service}
    if providers is not None:
        kwargs["provider_factory"] = lambda current: providers
    return ConsoleAPI(settings, **kwargs)


def _settings(tmp_path: Path, repo_root: Path, **overrides: Any):
    values: dict[str, Any] = {
        "music_enabled": True,
        "music_lyrics_dir": tmp_path / "music" / "lyrics",
        "music_cache_dir": tmp_path / "music" / "cache",
        "music_audio_dir": tmp_path / "audio",
        "netease_api_base": NETEASE,
        "kugou_api_base": KUGOU,
        "qq_api_base": QQ,
    }
    values.update(overrides)
    return make_settings(tmp_path, repo_root, **values)


@contextlib.contextmanager
def _running(api: ConsoleAPI) -> Iterator[ConsoleServer]:
    """起一台真服务（系统挑端口）。不走 ``build_server``，于是不会顺带装配 ``App``。"""

    server = ConsoleServer(("127.0.0.1", 0), api, quiet=True)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}
    )
    thread.start()
    try:
        yield server
    finally:
        # 先 ``shutdown()`` 再 ``close_all()``：直接关监听套接字会让还在 select 的
        # 线程抛 ``WinError 10038``（Task 6 的 test_music_http.py 已踩过，同款顺序）。
        server.shutdown()
        server.close_all()
        thread.join(timeout=5)


def _request(url: str) -> tuple[int, bytes]:
    """真请求：(状态码, 体)。4xx / 5xx 也照常回来，不抛异常。"""

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# --------------------------------------------------------------------- 网易云
NETEASE_SEARCH = {
    "result": {
        "songCount": 2,
        "songs": [
            {
                "id": 111,
                "name": "告别前要跳舞",
                "ar": [{"name": "汪苏泷"}],
                "al": {"name": "告别前要跳舞", "picUrl": "https://p1.music.126.net/a.jpg"},
                "dt": 240_000,
            },
            {
                "id": 222,
                "name": "跳",
                "ar": [{"name": "汪苏泷"}, {"name": "某某"}],
                "al": {"name": "专辑", "picUrl": ""},
                "dt": 180_000,
            },
        ],
    }
}


def test_netease_search_normalizes_songs():
    provider = NeteaseProvider(NETEASE, client=_client({"/cloudsearch": NETEASE_SEARCH}))

    page = provider.search_songs("告别前", limit=20, offset=0)

    assert page.total == 2
    assert [song.id for song in page.items] == ["111", "222"]
    assert page.items[0].title == "告别前要跳舞"
    assert page.items[0].artist == "汪苏泷"
    assert page.items[0].album == "告别前要跳舞"
    assert page.items[0].duration_ms == 240_000
    assert page.items[0].artwork_url == "https://p1.music.126.net/a.jpg"
    assert page.items[1].artist == "汪苏泷, 某某"
    assert page.has_more is False
    assert page.next_offset == 2


def test_netease_audio_source_reads_the_first_entry():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/song/url/v1")
        assert "level=exhigh" in str(request.url)
        return httpx.Response(200, json={"data": [{"url": "https://m8.music.126.net/x.mp3"}]})

    provider = NeteaseProvider(
        NETEASE, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    song = Song(provider="netease", id="111", title="告别前要跳舞")

    assert provider.get_audio_source(song) == "https://m8.music.126.net/x.mp3"


def test_netease_audio_source_reports_not_playable():
    provider = NeteaseProvider(NETEASE, client=_client({"/song/url/v1": {"data": [{"url": None}]}}))

    with pytest.raises(ProviderError) as exc:
        provider.get_audio_source(Song(provider="netease", id="111", title="会员曲"))

    assert exc.value.code == "not-playable"
    assert exc.value.provider == "netease"


def test_netease_lyrics_parse_the_plain_lrc():
    routes = {
        "/lyric/new": {
            "lrc": {"lyric": "[00:01.00]第一行\n[00:12.50]第二行\n"},
            "yrc": {"lyric": ""},
        }
    }
    provider = NeteaseProvider(NETEASE, client=_client(routes))

    result = provider.get_lyrics(Song(provider="netease", id="111", title="x"))

    assert result is not None
    assert [(line.time_ms, line.text) for line in result.lines] == [
        (1000, "第一行"),
        (12500, "第二行"),
    ]
    assert result.word_by_word is False

    # 网易云的 yrc（逐字）只在数据层记一笔；能进 Folia 依次渲染的是 QQ 的 QRC（取舍 5 / task-10）
    wordy = _client(
        {
            "/lyric/new": {
                "lrc": {"lyric": "[00:01.00]第一行\n"},
                "yrc": {"lyric": "[1000,500](0,500,0)第一行\n"},
            }
        }
    )
    result = NeteaseProvider(NETEASE, client=wordy).get_lyrics(
        Song(provider="netease", id="111", title="x")
    )
    assert result is not None and result.word_by_word is True


# --------------------------------------------------------------------- 酷狗
KUGOU_SEARCH = {
    "status": 1,
    "data": {
        "lists": [
            {
                "FileHash": "abcdef0123456789abcdef0123456789",
                "SongName": "告别前要跳舞",
                "SingerName": "汪苏泷",
                "AlbumName": "告别前要跳舞",
                "Duration": 240,
                "album_id": "4321",
                "album_audio_id": "98765",
            }
        ]
    },
}


def test_kugou_search_normalizes_songs():
    provider = KugouProvider(KUGOU, client=_client({"/search": KUGOU_SEARCH}))

    page = provider.search_songs("告别前", limit=20, offset=0)

    song = page.items[0]
    assert song.id == "ABCDEF0123456789ABCDEF0123456789"  # hash 一律大写
    assert song.title == "告别前要跳舞"
    assert song.artist == "汪苏泷"
    assert song.album == "告别前要跳舞"
    assert song.duration_ms == 240_000  # 秒 → 毫秒
    assert song.extra["album_id"] == "4321"
    assert song.extra["album_audio_id"] == "98765"


def test_kugou_audio_source_reads_the_url():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": {"url": "https://share.kugou.com/x.mp3"}})

    provider = KugouProvider(
        KUGOU, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    song = Song(
        provider="kugou",
        id="abcdEF",
        title="x",
        duration_ms=1000,
        extra={"album_id": "1", "album_audio_id": "2"},
    )

    assert provider.get_audio_source(song) == "https://share.kugou.com/x.mp3"
    assert "hash=ABCDEF" in seen[0]  # 大写 hash 才是有用的那个
    assert "quality=320" in seen[0]


def test_kugou_lyrics_walk_search_then_song():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/search/lyric"):
            assert "man=no" in str(request.url)
            return httpx.Response(200, json={"candidates": [{"id": 7, "accesskey": "key"}]})
        return httpx.Response(200, json={"data": {"decodeContent": "[00:01.00]第一行\n"}})

    provider = KugouProvider(
        KUGOU, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    song = Song(provider="kugou", id="abcDEF", title="x", duration_ms=240_000)

    result = provider.get_lyrics(song)

    assert seen == ["/search/lyric", "/lyric"]
    assert result is not None
    assert [(line.time_ms, line.text) for line in result.lines] == [(1000, "第一行")]


# --------------------------------------------------------------------- QQ
QQ_SEARCH = {
    "code": 0,
    "data": {
        "song": {
            "totalnum": 1,
            "curnum": 1,
            "list": [
                {
                    "mid": "0039MnYb0qxYhV",
                    "id": 123456,
                    "title": "告别前要跳舞",
                    "singer": [{"id": 1, "name": "汪苏泷"}],
                    "album": {"id": 2, "mid": "002fRO0N4FftzY", "name": "告别前要跳舞"},
                    "interval": 240,
                }
            ],
        }
    },
}


def test_qq_search_reads_the_legacy_soso_endpoint():
    """`DoSearchForQQMusicLite` 已废（``request.code=2001``、``item_song`` 是空列表），
    所以搜索换成 ``c.y.qq.com`` 的老入口——字段名一样，翻译层一行没改。"""

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=QQ_SEARCH)

    provider = QqProvider(QQ, client=httpx.Client(transport=httpx.MockTransport(handler)))

    page = provider.search_songs("告别前", limit=20, offset=0)

    assert seen[0].method == "GET"
    assert seen[0].url.host == "c.y.qq.com"
    assert seen[0].url.path == "/soso/fcgi-bin/client_search_cp"
    assert seen[0].url.params["w"] == "告别前"
    assert seen[0].headers["referer"] == "https://y.qq.com/"
    song = page.items[0]
    assert song.id == "0039MnYb0qxYhV"
    assert song.artist == "汪苏泷"
    assert song.album == "告别前要跳舞"
    assert song.duration_ms == 240_000
    assert page.total == 1
    assert page.has_more is False
    assert (
        song.artwork_url
        == "https://y.gtimg.cn/music/photo_new/T002R300x300M000002fRO0N4FftzY.jpg"
    )


def test_qq_audio_source_reads_play_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/getMusicPlay/" in request.url.path
        assert request.url.path.endswith("/0039MnYb0qxYhV")
        assert "quality=320" in str(request.url)
        return httpx.Response(
            200,
            json={
                "data": {
                    "playUrl": {
                        "0039MnYb0qxYhV": {"url": "https://isure.stream.qqmusic.qq.com/x.m4a"}
                    }
                }
            },
        )

    provider = QqProvider(QQ, client=httpx.Client(transport=httpx.MockTransport(handler)))
    song = Song(provider="qq", id="0039MnYb0qxYhV", title="x")

    assert provider.get_audio_source(song) == "https://isure.stream.qqmusic.qq.com/x.m4a"


def test_qq_lyrics_use_qrc_word_by_word():
    """QQ 的 QRC：3DES 解密 + raw inflate → 逐句 + 逐字 + 原样 ``karaoke_lyric``。

    「解得出」和「密文坏了」两条路径钉在同一个用例里：本任务用例总数因此仍是 19
    （``483 + 19 = 502``），不去牵动 task-08~11 的计数链。真要拆成两条，
    记得同步 task-08 / task-09 / task-10 / task-11 里所有 503 / 504 / 512 / 516。
    """

    plain = "[1234,5678]()你(0,200)好(200,300)\n[6912,4321]()再(0,150)见(150,250)"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"code": 0, "req_1": {"data": {"qrc": 1, "lyric": qrc_encrypt(plain)}}},
        )

    provider = QqProvider(QQ, client=httpx.Client(transport=httpx.MockTransport(handler)))

    result = provider.get_lyrics(Song(provider="qq", id="0039MnYb0qxYhV", title="x"))

    assert b"GetPlayLyricInfo" in seen[0].content
    # ``httpx.Request`` 没有 ``.json()``（那是 Response 的方法），自己解一遍 body。
    assert json.loads(seen[0].content)["req_1"]["param"]["songMID"] == "0039MnYb0qxYhV"
    assert result is not None
    assert result.word_by_word is True
    assert [line.text for line in result.lines] == ["你好", "再见"]
    assert [line.time_ms for line in result.lines] == [1234, 6912]
    # words 的口径：``(相对行首的毫秒偏移, 字)``——和 task-01 预留的那个 tuple 对齐。
    assert result.lines[0].words == ((0, "你"), (200, "好"))
    # 逐字原文原样带出，Folia（task-10）直接吃这串。
    assert result.karaoke_lyric == plain

    # 密文坏了 / 上游换算法：这会儿会再去要一次 qrc=0（见下一条用例）；这里连
    # qrc=0 也是同一份坏响应，所以最终空词 + 说明，不假装有词（取舍 5 的降级口径）。
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 0, "req_1": {"data": {"qrc": 1, "lyric": "AAAA"}}}
        )

    degraded = QqProvider(
        QQ, client=httpx.Client(transport=httpx.MockTransport(broken))
    ).get_lyrics(Song(provider="qq", id="0039MnYb0qxYhV", title="x"))

    assert degraded is not None
    assert degraded.lines == ()
    assert "空 LRC" in degraded.note


def test_qq_lyrics_fall_back_to_plain_lrc_when_qrc_is_broken():
    """QRC 解不开**不是**"这首歌没词"：同一个接口 ``qrc=0`` 给的是 base64 明文 LRC。

    真机上 ``qrc=1`` 回来的密文根本不是 8 的整数倍（7356 % 8 = 4），3DES 连门都进不去，
    所以这条降级是**主路径**——逐字没了，逐句照样有。
    """

    lrc = "[ti:晴天]\n[ar:周杰伦]\n[00:01.00]第一行\n[00:12.50]第二行\n"
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        qrc = json.loads(request.content)["req_1"]["param"]["qrc"]
        seen.append(qrc)
        if qrc == 1:
            return httpx.Response(
                200, json={"code": 0, "req_1": {"data": {"qrc": 1, "lyric": "AAAA"}}}
            )
        blob = base64.b64encode(lrc.encode("utf-8")).decode("ascii")
        return httpx.Response(
            200, json={"code": 0, "req_1": {"data": {"qrc": 0, "lyric": blob}}}
        )

    provider = QqProvider(QQ, client=httpx.Client(transport=httpx.MockTransport(handler)))

    result = provider.get_lyrics(Song(provider="qq", id="0039MnYb0qxYhV", title="x"))

    assert seen == [1, 0]  # 先试逐字，解不开再要逐句
    assert result is not None
    assert result.word_by_word is False
    assert result.karaoke_lyric == ""
    assert result.note == ""
    assert [(line.time_ms, line.text) for line in result.lines] == [
        (1000, "第一行"),
        (12500, "第二行"),
    ]


# SMTC 只给歌名 / 歌手，没有 songmid：`lyrics_for_track` 先搜一次再认。
QQ_TRACK_SEARCH = {
    "code": 0,
    "data": {
        "song": {
            "totalnum": 2,
            "curnum": 2,
            "list": [
                {  # 同名不同歌手：歌名对得上，歌手对不上，必须跳过
                    "mid": "000WRONGWRONG",
                    "title": "告别前要跳舞",
                    "singer": [{"name": "别人"}],
                    "album": {"mid": "000album000", "name": "别人的专辑"},
                    "interval": 240,
                },
                {
                    "mid": "0039MnYb0qxYhV",
                    "title": "告别前要跳舞 (Live)",
                    "singer": [{"name": "汪苏泷"}],
                    "album": {"mid": "002fRO0N4FftzY", "name": "告别前要跳舞"},
                    "interval": 241,
                },
            ],
        }
    },
}


def test_qq_lyrics_for_track_searches_then_picks_the_same_song():
    seen: list[httpx.Request] = []
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "c.y.qq.com":
            return httpx.Response(200, json=QQ_TRACK_SEARCH)
        asked.append(json.loads(request.content)["req_1"]["param"]["songMID"])
        return httpx.Response(
            200, json={"code": 0, "req_1": {"data": {"qrc": 0, "lyric": ""}}}
        )

    provider = QqProvider(QQ, client=httpx.Client(transport=httpx.MockTransport(handler)))
    track = Track(
        id="smtc:QQMusic|告别前要跳舞|汪苏泷",
        title="告别前要跳舞",
        artist="汪苏泷",
        duration_ms=240_000,
    )

    provider.lyrics_for_track(track)

    assert seen[0].url.params["w"] == "告别前要跳舞 汪苏泷"
    assert set(asked) == {"0039MnYb0qxYhV"}  # 跳过了同名不同歌手那条


def test_qq_lyrics_for_track_gives_up_rather_than_guessing():
    """对不上就回 ``None``：认错比没词更糟——那会把别人的词贴上这首歌。

    时长差得离谱（现场版 400 秒 vs 录音室 240 秒）也算对不上。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "song": {
                        "totalnum": 1,
                        "list": [
                            {
                                "mid": "000WRONGWRONG",
                                "title": "告别前要跳舞",
                                "singer": [{"name": "汪苏泷"}],
                                "album": {"mid": "000album000", "name": "x"},
                                "interval": 400,
                            }
                        ],
                    }
                },
            },
        )

    provider = QqProvider(QQ, client=httpx.Client(transport=httpx.MockTransport(handler)))
    track = Track(id="x", title="告别前要跳舞", artist="汪苏泷", duration_ms=240_000)

    assert provider.lyrics_for_track(track) is None
    # 歌名都空的情况下连搜都不该搜（省一次外呼）
    assert provider.lyrics_for_track(Track(id="y", title="   ")) is None


def test_build_music_service_wires_qq_into_the_playing_now_path(
    tmp_path: Path, repo_root: Path
):
    """"系统正在播放"那条链路原来只问本地 ``.lrc`` + LRCLib，所以中文歌常常一句词都没有。

    默认装配（``ConsoleAPI`` 用的就是它）必须把 QQ 接进去；这里塞一个假适配器把
    "接了没有 / 词有没有变成 ``qq``"整条钉死，一个字节不出网。
    """

    asked: list[str] = []

    class _FakeQq:
        def lyrics_for_track(self, track: Track) -> ProviderLyrics:
            asked.append(track.title)
            return ProviderLyrics(
                lines=(LyricLine(time_ms=1000, text="晴天"),), word_by_word=False
            )

    service = build_music_service(_settings(tmp_path, repo_root), qq=_FakeQq())

    outcome = service.resolve_lyrics(
        Track(id="x", title="晴天", artist="周杰伦", duration_ms=269_000)
    )

    assert asked == ["晴天"]
    assert outcome.status == "ready"
    assert outcome.source == "qq"
    assert [(line.time_ms, line.text) for line in outcome.lines] == [(1000, "晴天")]


# --------------------------------------------------------------------- registry
def test_unconfigured_provider_is_unavailable_not_broken():
    provider = NeteaseProvider("")

    assert provider.is_available() is False
    with pytest.raises(ProviderError) as exc:
        provider.search_songs("x")
    assert exc.value.code == "unavailable"
    assert exc.value.provider == "netease"


def test_unknown_provider_is_unsupported(tmp_path: Path, repo_root: Path):
    providers = build_providers(make_settings(tmp_path, repo_root))

    with pytest.raises(ProviderError) as exc:
        get_provider(providers, "spotify")
    assert exc.value.code == "unsupported"

    with pytest.raises(ProviderError) as exc:
        get_provider(providers, "netease")  # 有这个平台，但没配 base
    assert exc.value.code == "unavailable"


def test_registry_builds_all_three_and_reports_availability(
    tmp_path: Path, repo_root: Path
):
    settings = make_settings(
        tmp_path, repo_root, netease_api_base="http://n", kugou_api_base=""
    )

    providers = build_providers(settings)

    assert list(providers) == ["qq", "netease", "kugou"]
    assert providers["netease"].is_available() is True
    assert providers["kugou"].is_available() is False
    assert providers["qq"].is_available() is False


def test_provider_http_failure_maps_to_network():
    provider = NeteaseProvider(NETEASE, client=_client({}))  # 全 404

    with pytest.raises(ProviderError) as exc:
        provider.search_songs("x")

    assert exc.value.code == "network"
    assert exc.value.status == 404


def test_provider_401_maps_to_auth_required():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "login required"})

    provider = QqProvider(QQ, client=httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(ProviderError) as exc:
        provider.get_song_detail("0039MnYb0qxYhV")

    assert exc.value.code == "auth-required"
    assert exc.value.status == 401


# --------------------------------------------------------------------- HTTP 路由
def test_search_route_returns_hits(tmp_path: Path, repo_root: Path):
    settings = _settings(tmp_path, repo_root)
    providers = build_providers(settings, client=_client({"/cloudsearch": NETEASE_SEARCH}))
    api = _api(settings, providers=providers)
    query = urllib.parse.urlencode({"provider": "netease", "q": "告别前"})

    with _running(api) as server:
        status, raw = _request(server.url + "api/music/search?" + query)

    payload = json.loads(raw)
    assert status == 200
    assert payload["ok"] is True
    assert payload["provider"] == "netease"
    assert payload["items"][0]["title"] == "告别前要跳舞"
    assert payload["items"][0]["durationMs"] == 240_000
    assert payload["items"][0]["provider"] == "netease"


def test_search_route_needs_a_keyword(tmp_path: Path, repo_root: Path):
    settings = _settings(tmp_path, repo_root)
    api = _api(settings, providers=build_providers(settings))

    with _running(api) as server:
        status, raw = _request(server.url + "api/music/search?provider=netease&q=%20")

    payload = json.loads(raw)
    assert status == 400
    assert payload["error"] == "bad_query"


def test_song_route_returns_detail_lyrics_and_source(tmp_path: Path, repo_root: Path):
    settings = _settings(tmp_path, repo_root)
    routes = {
        "/song/detail": NETEASE_SEARCH["result"],
        "/song/url/v1": {"data": [{"url": "https://m8.music.126.net/x.mp3"}]},
        "/lyric/new": {"lrc": {"lyric": "[00:01.00]第一行\n"}, "yrc": {"lyric": ""}},
    }
    providers = build_providers(settings, client=_client(routes))
    api = _api(settings, providers=providers)

    with _running(api) as server:
        status, raw = _request(server.url + "api/music/song?provider=netease&id=111")

    payload = json.loads(raw)
    assert status == 200
    assert payload["song"]["title"] == "告别前要跳舞"
    assert payload["audioUrl"] == "https://m8.music.126.net/x.mp3"
    assert payload["lyrics"]["status"] == "ready"
    assert payload["lyrics"]["source"] == "netease"
    assert payload["lyrics"]["lines"] == [{"timeMs": 1000, "text": "第一行"}]
    # 逐字那两个键永远在（Task 5 的契约），这一家没 QRC 所以是空 + False。
    assert payload["lyrics"]["karaokeLyric"] == ""
    assert payload["lyrics"]["hasKaraokeLyric"] is False


def test_song_route_reports_an_unavailable_provider(tmp_path: Path, repo_root: Path):
    settings = _settings(tmp_path, repo_root, kugou_api_base="")
    api = _api(settings, providers=build_providers(settings))

    with _running(api) as server:
        status, raw = _request(server.url + "api/music/song?provider=kugou&id=abc")

    payload = json.loads(raw)
    assert status == 400
    assert payload["error"] == "unavailable"
