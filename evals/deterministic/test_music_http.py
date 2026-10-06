"""音乐链路的 HTTP 层：``/api/music/*`` 与 ``/api/audio/<name>``（Task 6）。

分两层，与 ``yixiang/web/`` 的两层一一对应：

  * ``parse_byte_range`` 是个纯函数：Range 头的三种形态、越界、多段、空文件全在
    这里钉死，不启服务就能断言；
  * ``ConsoleServer`` 起在 ``127.0.0.1:0`` 上，用真请求打一遍路由、状态码与响应头
    （拖动进度条靠的是 206 与 ``Content-Range``，只看 JSON 断言不到）。

**这一组用例不建 ``App``**：音乐链路根本不碰它（这正是设计取舍 1），所以直接
``ConsoleServer(...)`` 而不是 ``build_server(...)``——后者会 ``warmup()`` 一次 ``App``。
"""

from __future__ import annotations

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
from music_fakes import FakeControls, playing, session_factory

from yixiang.media.service import MusicService
from yixiang.web.console import ConsoleAPI, ConsoleError
from yixiang.web.server import ConsoleServer, parse_byte_range


# --------------------------------------------------------------------- 夹具
def _settings(tmp_path: Path, repo_root: Path, **overrides: Any):
    """音乐那几个目录一律落在 tmp_path：**别往仓库的 data/ 里写缓存**。"""

    values: dict[str, Any] = {
        "music_enabled": True,
        "music_lyrics_dir": tmp_path / "music" / "lyrics",
        "music_cache_dir": tmp_path / "music" / "cache",
        "music_audio_dir": tmp_path / "audio",
    }
    values.update(overrides)
    return make_settings(tmp_path, repo_root, **values)


def _no_lyrics() -> httpx.Client:
    """默认的歌词端点：一句"查不到"。用例不关心取词时用它，省得真打网络。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "no"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _api(
    settings,
    *,
    session: Any = None,
    lyrics: httpx.Client | None = None,
    app_factory: Any = None,
) -> ConsoleAPI:
    """一个控制台：音乐那一半全用假的（假 SMTC 会话 + 假 LRCLib + 钉死的时钟）。"""

    service = MusicService(
        settings,
        session_factory=session_factory(playing() if session is None else session),
        lyrics_client=lyrics if lyrics is not None else _no_lyrics(),
        clock=lambda: 1_786_000_000.0,
    )
    kwargs: dict[str, Any] = {"music_factory": lambda current: service}
    if app_factory is not None:
        kwargs["app_factory"] = app_factory
    return ConsoleAPI(settings, **kwargs)


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
        # 先 ``shutdown()``（让 ``serve_forever`` 那条线程自己退出循环）再 ``close_all()``：
        # 直接关监听套接字会让还在 select 的线程抛 ``WinError 10038``——仓库里既有的
        # web 用例都是这个顺序（test_web.py 的 ``_running`` 帮手）。
        server.shutdown()
        server.close_all()
        thread.join(timeout=5)


def _request(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any, bytes]:
    """真请求：(状态码, 响应头, 体)。4xx / 5xx 也照常回来，不抛异常。

    响应头是 ``email.message.Message``：大小写不敏感，用 ``headers.get("Content-Range")`` 读。
    """

    data: bytes | None = None
    merged = dict(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        merged.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, method=method, headers=merged)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _write_audio(tmp_path: Path, name: str, blob: bytes) -> Path:
    """在 ``tmp_path/audio`` 里放一个音频文件，回那个目录（`_settings` 的默认值）。"""

    directory = tmp_path / "audio"
    directory.mkdir(exist_ok=True)
    (directory / name).write_bytes(blob)
    return directory


def _query(**fields: Any) -> str:
    """真请求的 URL 只能是 ASCII：中文参数必须先转义（``parse_qs`` 会还原）。"""

    return urllib.parse.urlencode(fields)


# --------------------------------------------------------------------- Range（纯函数）
def test_parse_byte_range_maps_the_three_forms():
    assert parse_byte_range("bytes=0-99", 1000) == (0, 99)  # 闭区间
    assert parse_byte_range("bytes=500-", 1000) == (500, 999)  # 起点到末尾
    assert parse_byte_range("bytes=-100", 1000) == (900, 999)  # 末尾 100 字节
    assert parse_byte_range("bytes=0-999999", 1000) == (0, 999)  # end 夹到 size-1
    assert parse_byte_range("bytes=0-0", 1000) == (0, 0)  # 单个字节也要能要


def test_parse_byte_range_refuses_what_it_cannot_serve():
    assert parse_byte_range("bytes=1000-", 1000) is None  # 起点越界
    assert parse_byte_range("bytes=99-10", 1000) is None  # 反向区间
    assert parse_byte_range("bytes=-0", 1000) is None  # 零长后缀
    assert parse_byte_range("bytes=0-1,5-6", 1000) is None  # 多段：本机播放器用不到
    assert parse_byte_range("items=0-1", 1000) is None  # 不是 bytes 单位
    assert parse_byte_range("bytes=", 1000) is None  # 两头都空
    assert parse_byte_range("bytes=0-", 0) is None  # 空文件


# --------------------------------------------------------------------- 音乐路由
def test_music_state_route_returns_the_contract(tmp_path: Path, repo_root: Path):
    api = _api(_settings(tmp_path, repo_root))

    with _running(api) as server:
        status, _, raw = _request(server.url + "api/music/state")

    payload = json.loads(raw)
    assert status == 200
    assert payload["connection"] == "connected"
    assert payload["source"] == "smtc"
    assert payload["track"]["title"] == "告别前要跳舞"
    assert payload["playback"]["isPlaying"] is True
    assert payload["playback"]["positionMs"] == 30_000
    assert payload["pollMs"]["playing"] == 5000
    assert payload["lyrics"] == {
        "status": "empty",
        "source": "lrclib",
        "lines": [],
        "karaokeLyric": "",
        "hasKaraokeLyric": False,
    }


def test_music_state_route_is_disabled_when_music_is_off(
    tmp_path: Path, repo_root: Path
):
    api = _api(_settings(tmp_path, repo_root, music_enabled=False))

    with _running(api) as server:
        status, _, raw = _request(server.url + "api/music/state")

    payload = json.loads(raw)
    assert status == 200
    assert payload["connection"] == "disabled"
    assert payload["track"] is None
    assert payload["playback"]["isPlaying"] is False


def test_music_control_route_forwards_each_action(tmp_path: Path, repo_root: Path):
    session = playing()
    api = _api(_settings(tmp_path, repo_root), session=session)

    with _running(api) as server:
        status, _, raw = _request(
            server.url + "api/music/control", method="POST", body={"action": "pause"}
        )
        assert status == 200
        assert json.loads(raw) == {"ok": True, "applied": True}

        status, _, raw = _request(
            server.url + "api/music/control",
            method="POST",
            body={"action": "seek", "positionMs": 12_000},
        )
        assert status == 200
        assert json.loads(raw)["applied"] is True

    # seek 的 12 秒必须换算成 ticks（1 ms = 10 000 ticks）才交给 winrt
    assert session.calls == [("pause", None), ("seek", 120_000_000)]


def test_music_control_route_reports_a_missing_button_as_not_applied(
    tmp_path: Path, repo_root: Path
):
    session = playing(controls=FakeControls(is_next_enabled=True))
    api = _api(_settings(tmp_path, repo_root), session=session)

    with _running(api) as server:
        status, _, raw = _request(
            server.url + "api/music/control", method="POST", body={"action": "pause"}
        )

    payload = json.loads(raw)
    assert status == 200  # 按不动不是错误：界面据此灰掉按钮，而不是弹红字
    assert payload["ok"] is True
    assert payload["applied"] is False
    assert payload["reason"] == "not-applied"
    assert session.calls == []  # 没这个按钮就一次都不该碰 winrt


def test_music_control_route_rejects_an_unknown_action(
    tmp_path: Path, repo_root: Path
):
    api = _api(_settings(tmp_path, repo_root))

    with _running(api) as server:
        status, _, raw = _request(
            server.url + "api/music/control", method="POST", body={"action": "上一首"}
        )

    payload = json.loads(raw)
    assert status == 400
    assert payload["error"] == "bad_action"
    assert "上一首" in payload["message"]


def test_music_lyrics_route_takes_a_query(tmp_path: Path, repo_root: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"syncedLyrics": "[00:01.00]第一行\n[00:12.50]第二行\n"}
        )

    lyrics = httpx.Client(transport=httpx.MockTransport(handler))
    api = _api(_settings(tmp_path, repo_root), lyrics=lyrics)
    query = _query(title="告别前要跳舞", artist="汪苏泷", duration_ms=240_000)

    with _running(api) as server:
        status, _, raw = _request(server.url + "api/music/lyrics?" + query)

    payload = json.loads(raw)
    assert status == 200
    assert payload["status"] == "ready"
    assert payload["lines"] == [
        {"timeMs": 1000, "text": "第一行"},
        {"timeMs": 12500, "text": "第二行"},
    ]


def test_music_lyrics_route_needs_a_title(tmp_path: Path, repo_root: Path):
    api = _api(_settings(tmp_path, repo_root))

    with _running(api) as server:
        status, _, raw = _request(server.url + "api/music/lyrics?title=%20")

    payload = json.loads(raw)
    assert status == 400
    assert payload["error"] == "bad_query"


def test_music_library_route_lists_the_local_songs(tmp_path: Path, repo_root: Path):
    _write_audio(tmp_path, "b.mp3", b"bb")
    _write_audio(tmp_path, "a.wav", b"a")
    _write_audio(tmp_path, "notes.txt", b"not audio")
    api = _api(_settings(tmp_path, repo_root))

    with _running(api) as server:
        status, _, raw = _request(server.url + "api/music/library")

    items = json.loads(raw)["items"]
    assert status == 200
    assert [item["name"] for item in items] == ["a.wav", "b.mp3"]
    assert [item["url"] for item in items] == ["/api/audio/a.wav", "/api/audio/b.mp3"]
    assert items[0]["sizeBytes"] == 1


def test_music_routes_never_touch_the_app(tmp_path: Path, repo_root: Path):
    """音乐链路走请求线程：不装配 App，也不进那条唯一的业务工作线程。"""

    built: list[str] = []

    def refuse(current):
        built.append("app")
        raise AssertionError("音乐链路不该装配 App")

    api = _api(_settings(tmp_path, repo_root), app_factory=refuse)

    with _running(api) as server:
        assert _request(server.url + "api/music/state")[0] == 200
        assert _request(server.url + "api/music/library")[0] == 200
        assert (
            _request(
                server.url + "api/music/control",
                method="POST",
                body={"action": "pause"},
            )[0]
            == 200
        )

    assert built == []


# --------------------------------------------------------------------- 音频字节流
def test_audio_route_serves_audio_path_directly(tmp_path: Path, repo_root: Path):
    """`audio_path()` 自己那道门：只认曲库目录的直接子文件。"""

    directory = _write_audio(tmp_path, "hit.mp3", b"ok")
    (directory / "sub").mkdir()
    (directory / "sub" / "deep.mp3").write_bytes(b"ok")
    (directory / "notes.txt").write_bytes(b"not audio")
    api = _api(_settings(tmp_path, repo_root))

    assert api.audio_path("hit.mp3").name == "hit.mp3"
    for name in ("../.env", "sub/deep.mp3", "sub\\deep.mp3", "nope.mp3", "notes.txt"):
        with pytest.raises(ConsoleError) as exc:
            api.audio_path(name)
        assert exc.value.status == 404


def test_audio_route_streams_the_file_and_honours_head(
    tmp_path: Path, repo_root: Path
):
    blob = bytes(range(256)) * 4  # 1024 字节
    _write_audio(tmp_path, "hit.wav", blob)
    api = _api(_settings(tmp_path, repo_root))

    with _running(api) as server:
        status, headers, raw = _request(server.url + "api/audio/hit.wav")
        assert status == 200
        assert raw == blob
        assert headers.get("Content-Type") == "audio/wav"
        assert headers.get("Content-Length") == "1024"
        assert headers.get("Accept-Ranges") == "bytes"
        assert headers.get("Content-Range") is None

        # HEAD 只回头：前端据此知道"这首歌多大"，不白下整首
        status, headers, raw = _request(
            server.url + "api/audio/hit.wav", method="HEAD"
        )
        assert status == 200
        assert raw == b""
        assert headers.get("Content-Length") == "1024"


def test_audio_route_streams_a_range(tmp_path: Path, repo_root: Path):
    blob = bytes(range(256)) * 4
    _write_audio(tmp_path, "hit.mp3", blob)
    api = _api(_settings(tmp_path, repo_root))

    with _running(api) as server:
        status, headers, raw = _request(
            server.url + "api/audio/hit.mp3",
            headers={"Range": "bytes=10-19"},
            method="HEAD",
        )
        assert status == 206
        assert raw == b""
        assert headers.get("Content-Type") == "audio/mpeg"
        assert headers.get("Content-Length") == "10"
        assert headers.get("Content-Range") == "bytes 10-19/1024"
        assert headers.get("Accept-Ranges") == "bytes"

        status, headers, raw = _request(
            server.url + "api/audio/hit.mp3", headers={"Range": "bytes=10-19"}
        )
        assert status == 206
        assert raw == blob[10:20]

        status, headers, raw = _request(
            server.url + "api/audio/hit.mp3", headers={"Range": "bytes=-4"}
        )
        assert status == 206
        assert raw == blob[-4:]
        assert headers.get("Content-Range") == "bytes 1020-1023/1024"

        status, headers, raw = _request(
            server.url + "api/audio/hit.mp3", headers={"Range": "bytes=5000-"}
        )
        assert status == 416
        assert raw == b""
        assert headers.get("Content-Range") == "bytes */1024"


def test_audio_route_cannot_escape_the_audio_dir(tmp_path: Path, repo_root: Path):
    (tmp_path / "audio").mkdir()
    (tmp_path / ".env").write_text("YIXIANG_API_KEY=secret\n", encoding="utf-8")
    (tmp_path / "audio" / "notes.txt").write_text("不是音频", encoding="utf-8")
    api = _api(_settings(tmp_path, repo_root))

    with _running(api) as server:
        for path in (
            "/api/audio/..%2F.env",
            "/api/audio/%2e%2e%2f.env",
            "/api/audio/.env",
            "/api/audio/notes.txt",
            "/api/audio/nope.mp3",
            "/api/audio/",
        ):
            status, _, raw = _request(server.url.rstrip("/") + path)
            assert status == 404, path
            assert b"secret" not in raw


def test_audio_route_is_off_when_music_is_disabled(tmp_path: Path, repo_root: Path):
    blob = bytes(range(256)) * 4
    _write_audio(tmp_path, "hit.mp3", blob)
    api = _api(_settings(tmp_path, repo_root, music_enabled=False))

    with _running(api) as server:
        status, _, _ = _request(server.url + "api/audio/hit.mp3")

    assert status == 404
