"""音乐链路的测试替身：假 SMTC 会话 + 假 winrt 操作对象（Task 5 / 6 共用）。

形状照着 winrt 的真实形状搭（见 `test_music_smtc.py` 的模块说明）：
`get_playback_info()` / `get_timeline_properties()` 同步，`try_get_media_properties_async()`
回 awaitable，`source_app_user_model_id` 是属性不是方法。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any


class FakeOperation:
    """winrt 的异步操作：`smtc._await()` 靠它可 await 这一点取回结果。

    真 winrt 的 ``IAsyncOperation`` 同时支持 ``await`` 与 ``get_results()``；这里两个都留着，
    与 Task 3 那份假对象走的是同一条 await 路径。
    """

    def __init__(self, result: Any = None) -> None:
        self._result = result

    def get_results(self) -> Any:
        return self._result

    def __await__(self):
        async def inner() -> Any:
            return self._result

        return inner().__await__()


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
    def __init__(self, status: int = 4, controls: FakeControls | None = None) -> None:
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


class FakeSession:
    """一个"正在放的 app"：Task 3 的读方法 + Task 4 的可记录写方法。"""

    def __init__(
        self,
        *,
        title: str = "告别前要跳舞",
        artist: str = "汪苏泷",
        album: str = "",
        app_id: str = "QQMusic.exe",
        status: int = 4,
        position_ms: int = 30_000,
        duration_ms: int = 240_000,
        controls: FakeControls | None = None,
        results: dict[str, bool] | None = None,
    ) -> None:
        self.source_app_user_model_id = app_id
        self._info = FakePlaybackInfo(status, controls)
        self._timeline = FakeTimeline(position_ms=position_ms, end_ms=duration_ms)
        self._media = FakeMedia(title, artist, album)
        self.calls: list[tuple[str, int | None]] = []
        self._results = dict(results or {})

    # ---- 读（Task 3）
    def get_playback_info(self) -> FakePlaybackInfo:
        return self._info

    def get_timeline_properties(self) -> FakeTimeline:
        return self._timeline

    def try_get_media_properties_async(self) -> FakeOperation:
        return FakeOperation(self._media)

    # ---- 写（Task 4）
    def _record(self, name: str, value: int | None = None) -> FakeOperation:
        self.calls.append((name, value))
        return FakeOperation(self._results.get(name, True))

    def try_play_async(self) -> FakeOperation:
        return self._record("play")

    def try_pause_async(self) -> FakeOperation:
        return self._record("pause")

    def try_skip_next_async(self) -> FakeOperation:
        return self._record("next")

    def try_skip_previous_async(self) -> FakeOperation:
        return self._record("previous")

    def try_change_playback_position_async(self, ticks: int) -> FakeOperation:
        return self._record("seek", ticks)


def playing(**overrides: Any) -> FakeSession:
    """一首正在放的歌：QQ 音乐 / 告别前要跳舞 / 30 秒处 / 五个按钮都亮。

    "五个"含 ``is_playback_position_enabled``（拖进度条）：Task 4 的假会话就是这么
    写的，QQ 音乐真机也是这个形状。少这一个，``control("seek")`` 会静默回
    ``applied=False``（"这个 app 没这个按钮"），HTTP 那条链路的 seek 用例就过不去。
    """

    options: dict[str, Any] = {
        "controls": FakeControls(
            is_play_enabled=True,
            is_pause_enabled=True,
            is_next_enabled=True,
            is_previous_enabled=True,
            is_playback_position_enabled=True,
        )
    }
    options.update(overrides)
    return FakeSession(**options)


def paused(**overrides: Any) -> FakeSession:
    return playing(status=5, **overrides)


def session_factory(*sessions: Any) -> Callable[[], Any]:
    """`MusicService(session_factory=...)` 的桩：按顺序发；发完一直回最后一个；空则回 None。"""

    queue = list(sessions)

    def factory() -> Any:
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0] if queue else None

    return factory
