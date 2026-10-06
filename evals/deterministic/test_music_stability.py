"""`MusicService` 的两处稳定化：短暂读不到 SMTC 时保持上一首、以及舞台问进度。

用户报的"音乐播放到一半会突然消失"是这两件事叠出来的：

  * QQ 音乐切歌 / 最小化 / 会话重建的那一两秒里，``get_current_session()`` 会短暂回
    ``None``（或让 winrt 抛一个不是 ``SmtcUnavailable`` 的异常）。原来的代码当场把
    整块状态清空，面板与歌词舞台就一起闪一下——看起来就是"突然消失，无法识别"。
  * 歌词舞台在嵌入模式下按秒级打 ``/api/spotify/progress``；那个数不能每次都去问
    一遍 winrt，所以拿上一次 ``state()`` 的快照按流逝往前推。

时钟是注入的，所以"过了 8 秒"这种断言不用真的等 8 秒。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from conftest import make_settings
from music_fakes import paused, playing

from yixiang.media.service import HOLD_MS, LYRICS_ERROR_RETRY_MS, MusicService
from yixiang.media.smtc import SmtcUnavailable


class _Clock:
    """可推的假时钟：`MusicService` 只认 ``clock()`` 这一个接口。"""

    def __init__(self, start: float = 1_786_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _lyrics_client(payload: Any, *, status: int = 200, counter: list[int] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if counter is not None:
            counter.append(1)
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _service(
    tmp_path: Path,
    repo_root: Path,
    *,
    session: Any,
    clock: _Clock,
    client: httpx.Client | None = None,
) -> MusicService:
    return MusicService(
        make_settings(
            tmp_path,
            repo_root,
            music_enabled=True,
            music_lyrics_dir=tmp_path / "lyrics",
            music_cache_dir=tmp_path / "cache",
            music_audio_dir=tmp_path / "audio",
        ),
        session_factory=session,
        lyrics_client=client
        if client is not None
        else _lyrics_client({"message": "no"}, status=404),
        clock=clock,
    )


def _flaky(*sessions: Any):
    """先按顺序发，发完一直回最后一个（``None`` 就是"这一秒读不到"）。"""

    queue = list(sessions)

    def factory() -> Any:
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0] if queue else None

    return factory


class _Broken:
    """一个"一读就炸"的会话：winrt 在会话失效 / 系统没起来时就是这么抛的。

    假会话的异常要**从 ``get_playback_info()`` 里抛**（``read_current`` 不会自己吞异常），
    所以这里不能用"会抛异常的函数"顶替会话——那只会变成一句 ``AttributeError``。
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def get_playback_info(self) -> Any:
        raise self._exc


# --------------------------------------------------------------- 保持上一首
def test_a_short_smtc_gap_keeps_the_last_track(tmp_path: Path, repo_root: Path):
    clock = _Clock()
    service = _service(
        tmp_path, repo_root, session=_flaky(playing(), None), clock=clock
    )

    first = service.state()
    clock.advance(1.0)
    held = service.state()

    assert first["connection"] == "connected"
    assert held["connection"] == "connected"
    assert held["track"]["title"] == first["track"]["title"]
    assert held["playback"]["isPlaying"] is True


def test_holding_leaves_the_sample_time_alone(tmp_path: Path, repo_root: Path):
    """采样时刻不动，前端按"位置 + 流逝"算出来的进度条才会照常走。"""

    clock = _Clock()
    service = _service(
        tmp_path, repo_root, session=_flaky(playing(), None), clock=clock
    )

    first = service.state()
    clock.advance(4.0)
    held = service.state()

    assert held["playback"]["sampledAtMs"] == first["playback"]["sampledAtMs"]
    assert held["playback"]["positionMs"] == first["playback"]["positionMs"]


def test_the_gap_must_stay_short(tmp_path: Path, repo_root: Path):
    clock = _Clock()
    service = _service(
        tmp_path, repo_root, session=_flaky(playing(), None), clock=clock
    )

    service.state()
    clock.advance(HOLD_MS / 1000 + 1.0)
    state = service.state()

    assert state["connection"] == "offline"
    assert state["track"] is None
    assert "note" not in state  # "没在放"不是错，别硬塞一句报错


def test_a_session_that_reports_no_title_counts_as_a_gap(tmp_path: Path, repo_root: Path):
    """QQ 音乐切歌那一两秒里 SMTC 会回一条空壳会话（有会话、歌名是空的）。

    歌名一空就等于"不知道放的是哪首"：面板会闪、歌词舞台会回占位文案。所以这种
    快照也走同一个宽限窗口，而不是当场把整块状态清掉。
    """

    clock = _Clock()
    service = _service(
        tmp_path, repo_root, session=_flaky(playing(), playing(title="")), clock=clock
    )

    first = service.state()
    clock.advance(1.0)
    held = service.state()

    assert held["connection"] == "connected"
    assert held["track"]["title"] == first["track"]["title"]


def test_a_real_smtc_failure_after_the_gap_says_what_broke(
    tmp_path: Path, repo_root: Path
):
    clock = _Clock()
    service = _service(
        tmp_path,
        repo_root,
        session=_flaky(playing(), _Broken(SmtcUnavailable("SMTC 管理器不可用"))),
        clock=clock,
    )

    service.state()
    clock.advance(HOLD_MS / 1000 + 1.0)
    state = service.state()

    assert state["connection"] == "offline"
    assert "SMTC 管理器不可用" in state["note"]


def test_a_wild_exception_from_winrt_is_treated_as_a_gap(
    tmp_path: Path, repo_root: Path
):
    """会话失效时 winrt 抛的类型随版本变，别让它冒到 HTTP 层变成一个 500。"""

    clock = _Clock()
    service = _service(
        tmp_path,
        repo_root,
        session=_flaky(playing(), _Broken(RuntimeError("元素未找到。"))),
        clock=clock,
    )

    first = service.state()
    clock.advance(1.0)

    assert service.state()["track"]["title"] == first["track"]["title"]


# ------------------------------------------------------------------- 进度
def test_progress_advances_with_the_wall_clock_while_playing(
    tmp_path: Path, repo_root: Path
):
    clock = _Clock()
    service = _service(tmp_path, repo_root, session=_flaky(playing()), clock=clock)

    assert service.state()["playback"]["positionMs"] == 30_000
    assert service.progress_ms() == 30_000  # 还没过时间
    clock.advance(2.5)

    assert service.progress_ms() == 32_500


def test_progress_stands_still_when_paused(tmp_path: Path, repo_root: Path):
    clock = _Clock()
    service = _service(tmp_path, repo_root, session=_flaky(paused()), clock=clock)

    service.state()
    clock.advance(30.0)

    assert service.progress_ms() == 30_000  # 暂停就是那个位置本身，不跟着时钟走


def test_progress_before_anything_was_read_is_zero(tmp_path: Path, repo_root: Path):
    clock = _Clock()
    service = _service(tmp_path, repo_root, session=_flaky(None), clock=clock)

    assert service.progress_ms() == 0


# ------------------------------------------------------------------- 取词失败
def test_a_failed_lyrics_lookup_is_retried_after_the_window(
    tmp_path: Path, repo_root: Path
):
    counter: list[int] = []
    clock = _Clock()
    service = _service(
        tmp_path,
        repo_root,
        session=_flaky(playing()),
        clock=clock,
        client=_lyrics_client({"message": "boom"}, status=502, counter=counter),
    )

    assert service.state()["lyrics"]["status"] == "error"
    clock.advance(1.0)
    assert service.state()["lyrics"]["status"] == "error"  # 窗口内：不回退到网络
    assert counter == [1]

    clock.advance(LYRICS_ERROR_RETRY_MS / 1000)
    assert service.state()["lyrics"]["status"] == "error"
    assert counter == [1, 1]  # 窗口过了：重问一次

    clock.advance(1.0)
    assert service.state()["lyrics"]["status"] == "error"
    assert counter == [1, 1]  # 问过了就再等一个窗口


def test_a_good_lyrics_answer_is_never_asked_twice(tmp_path: Path, repo_root: Path):
    counter: list[int] = []
    clock = _Clock()
    service = _service(
        tmp_path,
        repo_root,
        session=_flaky(playing()),
        clock=clock,
        client=_lyrics_client(
            {"syncedLyrics": "[00:01.00]第一行\n"}, counter=counter
        ),
    )

    assert service.state()["lyrics"]["status"] == "ready"
    clock.advance(LYRICS_ERROR_RETRY_MS / 1000 * 10)
    assert service.state()["lyrics"]["status"] == "ready"
    assert counter == [1]
