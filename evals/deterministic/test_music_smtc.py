"""Windows SMTC：读当前会话（Task 3）+ 反向控制（Task 4）。

一律用**假 session**：真 SMTC 在沙箱里直接 `WinError -2147023836`（"指定的服务未安装"），
离线跑不了，只能真机手验。所以这一层的假对象要**照着 winrt 的真实形状**搭：
`get_playback_info()` / `get_timeline_properties()` 是同步的，
`try_get_media_properties_async()` 返回 awaitable，`source_app_user_model_id` 是属性不是方法。
"""

from __future__ import annotations

import datetime as dt

import pytest

from yixiang.media import smtc
from yixiang.media.smtc import SmtcSnapshot, control, ms_of, read_current


class FakeControls:
    """只回名字里带 ``is_`` 的开关；没显式给的一律 False（真实 winrt 也是布尔属性）。"""

    def __init__(self, **flags: bool) -> None:
        for name, value in flags.items():
            setattr(self, name, value)

    def __getattr__(self, name: str) -> bool:
        if name.startswith("is_"):
            return False
        raise AttributeError(name)


class FakePlaybackInfo:
    def __init__(self, status: int, controls: FakeControls | None = None) -> None:
        self.playback_status = status
        self.controls = controls or FakeControls()


class FakeTimeline:
    def __init__(self, *, position_ms: int = 0, start_ms: int = 0, end_ms: int = 0) -> None:
        self.position = dt.timedelta(milliseconds=position_ms)
        self.start_time = dt.timedelta(milliseconds=start_ms)
        self.end_time = dt.timedelta(milliseconds=end_ms)


class FakeMedia:
    def __init__(self, title: str = "", artist: str = "", album: str = "") -> None:
        self.title = title
        self.artist = artist
        self.album_title = album


class FakeOperation:
    """像 winrt 的 ``IAsyncOperation`` 一样可以 await（顺带盖住 ``_await`` 的异步分支）。"""

    def __init__(self, value: object) -> None:
        self._value = value

    def __await__(self):
        async def inner() -> object:
            return self._value

        return inner().__await__()


class FakeSession:
    """一个"正在放的 app"。读方法照 Task 3；Task 4 起多一套可记录的 try_ 写方法。"""

    def __init__(
        self,
        *,
        status: int = 4,
        controls: FakeControls | None = None,
        timeline: FakeTimeline | None = None,
        media: FakeMedia | None = None,
        app_id: str = "QQMusic.exe",
        media_is_awaitable: bool = False,
        calls: list[tuple[str, int | None]] | None = None,
        results: dict[str, bool] | None = None,
    ) -> None:
        self._info = FakePlaybackInfo(status, controls)
        self._timeline = timeline or FakeTimeline()
        self._media = media
        self.source_app_user_model_id = app_id
        self._media_is_awaitable = media_is_awaitable
        self.calls: list[tuple[str, int | None]] = [] if calls is None else calls
        self._results: dict[str, bool] = {} if results is None else results

    def get_playback_info(self) -> FakePlaybackInfo:
        return self._info

    def get_timeline_properties(self) -> FakeTimeline:
        return self._timeline

    def try_get_media_properties_async(self) -> object:
        if self._media_is_awaitable:
            return FakeOperation(self._media)
        return self._media

    # --- Task 4：写方法。真实 winrt 这几支都返回 IAsyncOperation[bool]，
    # 拒绝时是"回了 False"而不是抛异常，所以这里也照抄：记录参数 + 回 FakeOperation。
    def _record(self, name: str, argument: int | None = None) -> FakeOperation:
        self.calls.append((name, argument))
        return FakeOperation(self._results.get(name, True))

    def try_play_async(self) -> FakeOperation:
        return self._record("play")

    def try_pause_async(self) -> FakeOperation:
        return self._record("pause")

    def try_skip_next_async(self) -> FakeOperation:
        return self._record("next")

    def try_skip_previous_async(self) -> FakeOperation:
        return self._record("previous")

    def try_change_playback_position_async(self, requested_playback_position: int) -> FakeOperation:
        return self._record("seek", requested_playback_position)


def _read(**kwargs: object) -> SmtcSnapshot | None:
    """建一个假 session 塞进 ``read_current``：所有用例都从这里进。"""

    session = FakeSession(**kwargs)  # type: ignore[arg-type]
    return read_current(lambda: session)


def test_read_current_maps_a_playing_session():
    snapshot = _read(
        status=4,
        controls=FakeControls(is_play_enabled=True, is_next_enabled=True),
        timeline=FakeTimeline(position_ms=12_000, end_ms=245_400),
        media=FakeMedia(title="告别前要跳舞", artist="汪苏泷", album="21世纪罗曼史"),
        app_id="QQMusic.exe",
        media_is_awaitable=True,
    )

    assert snapshot is not None
    assert snapshot.status == "playing"          # 4 = PLAYING（本机实测）
    assert snapshot.app_id == "QQMusic.exe"
    assert snapshot.track.title == "告别前要跳舞"
    assert snapshot.track.artist == "汪苏泷"
    assert snapshot.track.album == "21世纪罗曼史"
    assert snapshot.track.duration_ms == 245_400
    assert snapshot.position_ms == 12_000
    assert snapshot.controls == ("play", "next")
    assert snapshot.track.id.startswith("smtc:QQMusic.exe|")


def test_status_codes_map_to_names_and_unknown_codes_fall_back():
    for raw, expected in (
        (4, "playing"),
        (5, "paused"),
        (3, "stopped"),
        (0, "unknown"),
        (9, "unknown"),  # 将来 winrt 加了新状态：不认识就是 unknown，别抛
    ):
        snapshot = _read(status=raw)
        assert snapshot is not None
        assert snapshot.status == expected


def test_position_is_relative_to_start_time_and_never_negative():
    snapshot = _read(timeline=FakeTimeline(position_ms=30_000, start_ms=5_000, end_ms=200_000))

    assert snapshot is not None
    assert snapshot.position_ms == 25_000   # 30_000 - 5_000
    assert snapshot.duration_ms == 195_000  # 200_000 - 5_000

    # 播放器把 position 报到了 start_time 之前：夹到 0，别让进度条往回跳
    assert _read(timeline=FakeTimeline(position_ms=1_000, start_ms=5_000)).position_ms == 0

    assert ms_of(dt.timedelta(seconds=2)) == 2_000
    assert ms_of(dt.timedelta(milliseconds=1534)) == 1534
    assert ms_of(None) == 0
    assert ms_of(-5) == 0
    assert ms_of("不是时间") == 0


def test_controls_tuple_lists_only_enabled_buttons():
    enabled = FakeControls(
        is_play_enabled=True,
        is_pause_enabled=False,
        is_next_enabled=True,
        is_previous_enabled=False,
    )

    snapshot = _read(controls=enabled)

    assert snapshot is not None
    assert snapshot.controls == ("play", "next")
    assert _read(controls=FakeControls()).controls == ()


def test_read_current_returns_none_without_a_session():
    assert read_current(lambda: None) is None


def test_read_current_tolerates_missing_metadata():
    # 有些 app 注册了会话但还没报元数据：标题给空串，链路的其它部分照样能跑
    snapshot = _read(media=None, app_id="")

    assert snapshot is not None
    assert snapshot.track.title == ""
    assert snapshot.track.artist == ""
    assert snapshot.track.duration_ms == 0
    assert snapshot.app_id == ""
    assert snapshot.track.id == "smtc:"


def test_read_current_degrades_to_none_when_winrt_is_missing(monkeypatch: pytest.MonkeyPatch):
    # 没装 winrt（或不是 Windows）：默认工厂返回 None，不是异常
    monkeypatch.setattr(smtc, "_SessionManager", None)

    assert read_current() is None


# 五个按钮都亮着 = 一个"什么都能干"的播放器（QQ 音乐真机就是这个形状）
ALL_CONTROLS = FakeControls(
    is_play_enabled=True,
    is_pause_enabled=True,
    is_next_enabled=True,
    is_previous_enabled=True,
    is_playback_position_enabled=True,
)


def _controllable(**kwargs: object) -> FakeSession:
    """默认给一套全亮的按钮；显式传了 controls= 就以显式的为准。"""

    kwargs.setdefault("controls", ALL_CONTROLS)
    return FakeSession(**kwargs)  # type: ignore[arg-type]


def test_control_play_pause_and_skip_call_the_matching_winrt_method():
    session = _controllable()

    assert control("play", session_factory=lambda: session) is True
    assert control("pause", session_factory=lambda: session) is True
    assert control("next", session_factory=lambda: session) is True
    assert control("previous", session_factory=lambda: session) is True

    # 方法名是 skip_next / skip_previous，不是 try_next_async / try_previous_async
    assert [name for name, _ in session.calls] == ["play", "pause", "next", "previous"]


def test_seek_converts_milliseconds_to_ticks():
    # 微软文档：TryChangePlaybackPositionAsync 的单位是 ticks（100 ns）→ 1 ms = 10 000 ticks
    session = _controllable()

    assert control("seek", position_ms=60_000, session_factory=lambda: session) is True

    assert session.calls == [("seek", 600_000_000)]


def test_seek_clamps_a_negative_position_to_zero():
    session = _controllable()

    assert control("seek", position_ms=-5, session_factory=lambda: session) is True

    assert session.calls == [("seek", 0)]


def test_control_returns_false_for_a_button_this_app_does_not_have():
    # 界面亮不亮由 controls 决定：没这个按钮就根本不去调 winrt
    session = _controllable(controls=FakeControls(is_play_enabled=True))

    assert control("next", session_factory=lambda: session) is False
    assert control("seek", position_ms=1_000, session_factory=lambda: session) is False
    assert session.calls == []


def test_control_propagates_a_winrt_refusal():
    # 按钮亮着，按下去 winrt 仍可能回 False（正在缓冲 / 设备被独占）：照实回 False，不抛
    session = _controllable(results={"play": False})

    assert control("play", session_factory=lambda: session) is False
    assert [name for name, _ in session.calls] == ["play"]


def test_control_returns_false_without_a_session():
    assert control("play", session_factory=lambda: None) is False


def test_control_rejects_an_unknown_action():
    session = _controllable()

    with pytest.raises(ValueError):
        control("音量调到最大", session_factory=lambda: session)

    assert session.calls == []
