"""MusicService：状态 JSON、歌词降级、控制转发、本机曲库（Task 5）。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import make_settings
from music_fakes import FakeControls, paused, playing, session_factory

from yixiang.media.lyrics import LyricLine, Lyrics
from yixiang.media.service import (
    MIN_POLL_MS,
    LyricsOutcome,
    MusicService,
    lyrics_json,
    track_from_query,
)
from yixiang.media.smtc import SmtcUnavailable


def _client(payload: object, *, status: int = 200, counter: list[int] | None = None):
    """一个 LRCLib 假端点：`counter` 传进来就能数"到底问了几次"。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if counter is not None:
            counter.append(1)
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _synced(*lines: str) -> dict[str, object]:
    return {"syncedLyrics": "\n".join(lines) + "\n", "plainLyrics": "没有时间轴"}


def _service(tmp_path: Path, repo_root: Path, *, session=None, client=None, **overrides):
    """一个音乐服务：目录全在 tmp_path，歌词一律走假端点（默认"查不到"），时钟钉死。"""

    values: dict[str, object] = {
        "music_enabled": True,
        "music_lyrics_dir": tmp_path / "lyrics",
        "music_cache_dir": tmp_path / "cache",
        "music_audio_dir": tmp_path / "audio",
    }
    values.update(overrides)
    return MusicService(
        make_settings(tmp_path, repo_root, **values),
        session_factory=session,
        lyrics_client=client if client is not None else _client({"message": "no"}, status=404),
        clock=lambda: 1_786_000_000.0,
    )


def test_music_disabled_is_a_quiet_empty_state(tmp_path: Path, repo_root: Path):
    service = _service(tmp_path, repo_root, music_enabled=False)

    state = service.state()

    assert state["ok"] is True
    assert state["connection"] == "disabled"
    assert state["track"] is None
    assert state["lyrics"]["status"] == "empty"
    assert state["playback"]["isPlaying"] is False


def test_state_maps_the_smtc_session_into_the_contract(tmp_path: Path, repo_root: Path):
    service = _service(tmp_path, repo_root, session=session_factory(playing()))

    state = service.state()

    assert state["connection"] == "connected"
    assert state["source"] == "smtc"
    assert state["track"]["title"] == "告别前要跳舞"
    assert state["track"]["artist"] == "汪苏泷"
    assert state["track"]["durationMs"] == 240_000
    assert state["track"]["id"].startswith("smtc:")
    assert state["playback"]["isPlaying"] is True
    assert state["playback"]["positionMs"] == 30_000
    assert state["playback"]["sampledAtMs"] == 1_786_000_000_000
    assert state["playback"]["controls"] == ["play", "pause", "next", "previous"]
    assert state["lyrics"] == {
        "status": "empty",
        "source": "lrclib",
        "lines": [],
        "karaokeLyric": "",
        "hasKaraokeLyric": False,
    }


def test_a_paused_session_reports_not_playing(tmp_path: Path, repo_root: Path):
    service = _service(tmp_path, repo_root, session=session_factory(paused()))

    assert service.state()["playback"]["isPlaying"] is False


def test_position_is_clamped_into_the_track_duration(tmp_path: Path, repo_root: Path):
    session = playing(position_ms=90_000, duration_ms=60_000)
    service = _service(tmp_path, repo_root, session=session_factory(session))

    state = service.state()

    assert state["playback"]["positionMs"] == 60_000


def test_no_session_at_all_is_an_offline_state(tmp_path: Path, repo_root: Path):
    service = _service(tmp_path, repo_root, session=session_factory())

    state = service.state()

    assert state["connection"] == "offline"
    assert state["track"] is None


def test_smtc_failure_becomes_an_offline_state_with_a_note(
    tmp_path: Path, repo_root: Path
):
    def boom() -> None:
        raise SmtcUnavailable("SMTC 管理器不可用")

    service = _service(tmp_path, repo_root, session=boom)

    state = service.state()

    assert state["connection"] == "offline"
    assert "SMTC 管理器不可用" in state["note"]


def test_a_local_track_reports_local_audition(tmp_path: Path, repo_root: Path):
    service = _service(tmp_path, repo_root, session=session_factory(playing()))

    state = service.state(track=track_from_query("本机的一首", "本地"))

    assert state["connection"] == "local"
    assert state["source"] == "local-audition"
    assert state["track"]["title"] == "本机的一首"
    assert state["playback"]["isPlaying"] is True
    assert state["lyrics"]["status"] == "empty"


def test_poll_intervals_are_clamped_to_the_minimum(tmp_path: Path, repo_root: Path):
    service = _service(
        tmp_path,
        repo_root,
        session=session_factory(),
        music_poll_playing_ms=0,
        music_poll_near_end_ms=10,
    )

    poll = service.state()["pollMs"]

    assert poll["playing"] == MIN_POLL_MS
    assert poll["nearEnd"] == MIN_POLL_MS
    assert poll["paused"] == 15_000
    assert poll["idle"] == 30_000


def test_a_local_lrc_file_wins_over_lrclib(tmp_path: Path, repo_root: Path):
    lyrics_dir = tmp_path / "lyrics"
    lyrics_dir.mkdir()
    (lyrics_dir / "汪苏泷 - 告别前要跳舞.lrc").write_text("[00:01.00]本地的词\n", encoding="utf-8")
    counter: list[int] = []
    service = _service(
        tmp_path,
        repo_root,
        session=session_factory(playing()),
        client=_client(_synced("[00:01.00]网络上的词"), counter=counter),
    )

    state = service.state()

    assert state["lyrics"]["status"] == "ready"
    assert state["lyrics"]["source"] == "lrc-file"
    assert state["lyrics"]["lines"] == [{"timeMs": 1000, "text": "本地的词"}]
    assert counter == []


def test_lrclib_is_asked_exactly_once_per_track(tmp_path: Path, repo_root: Path):
    counter: list[int] = []
    service = _service(
        tmp_path,
        repo_root,
        session=session_factory(playing()),
        client=_client(_synced("[00:01.00]第一行", "[00:05.00]第二行"), counter=counter),
    )

    first = service.state()["lyrics"]
    second = service.state()["lyrics"]

    assert first == second
    assert first["status"] == "ready"
    assert first["source"] == "lrclib"
    assert first["lines"] == [
        {"timeMs": 1000, "text": "第一行"},
        {"timeMs": 5000, "text": "第二行"},
    ]
    assert counter == [1]


def test_a_track_without_lyrics_is_reported_empty_and_asked_once(
    tmp_path: Path, repo_root: Path
):
    counter: list[int] = []
    service = _service(
        tmp_path,
        repo_root,
        session=session_factory(playing()),
        client=_client({"message": "no"}, status=404, counter=counter),
    )

    assert service.state()["lyrics"]["status"] == "empty"
    assert service.state()["lyrics"]["status"] == "empty"
    assert counter == [1]


def test_a_broken_lrclib_does_not_break_the_playback_state(tmp_path: Path, repo_root: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("不接")

    service = _service(
        tmp_path,
        repo_root,
        session=session_factory(playing()),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    state = service.state()

    assert state["connection"] == "connected"
    assert state["playback"]["isPlaying"] is True
    assert state["lyrics"]["status"] == "error"
    assert "LRCLib" in state["lyrics"]["note"]


def test_control_forwards_to_the_session_and_reports_applied(tmp_path: Path, repo_root: Path):
    session = playing()
    service = _service(tmp_path, repo_root, session=session_factory(session))

    result = service.control("play")

    assert result["applied"] is True
    assert session.calls == [("play", None)]


def test_control_on_a_session_without_that_button_is_not_applied(
    tmp_path: Path, repo_root: Path
):
    session = playing(controls=FakeControls(is_next_enabled=True))
    service = _service(tmp_path, repo_root, session=session_factory(session))

    result = service.control("play")

    assert result["ok"] is True
    assert result["applied"] is False
    assert result["reason"] == "not-applied"
    assert session.calls == []


def test_control_when_music_is_disabled_says_so(tmp_path: Path, repo_root: Path):
    session = playing()
    service = _service(
        tmp_path, repo_root, music_enabled=False, session=session_factory(session)
    )

    result = service.control("pause")

    assert result["applied"] is False
    assert result["reason"] == "disabled"
    assert session.calls == []


def test_control_when_smtc_blows_up_says_so(tmp_path: Path, repo_root: Path):
    def boom() -> None:
        raise SmtcUnavailable("没装 winrt")

    service = _service(tmp_path, repo_root, session=boom)

    result = service.control("pause")

    assert result["applied"] is False
    assert result["reason"] == "smtc-unavailable"


def test_control_rejects_an_unknown_action(tmp_path: Path, repo_root: Path):
    service = _service(tmp_path, repo_root, session=session_factory(playing()))

    with pytest.raises(ValueError):
        service.control("上一首")


def test_library_lists_audio_files_sorted_with_encoded_urls(
    tmp_path: Path, repo_root: Path
):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "b 歌.mp3").write_bytes(b"ID3")
    (audio_dir / "a歌.mp3").write_bytes(b"ID3")
    (audio_dir / "说明.txt").write_text("不是音频", encoding="utf-8")
    service = _service(tmp_path, repo_root)

    items = service.library()["items"]

    assert [item["name"] for item in items] == ["a歌.mp3", "b 歌.mp3"]
    assert items[1]["url"] == "/api/audio/b%20%E6%AD%8C.mp3"
    assert items[0]["sizeBytes"] == 3


def test_library_is_empty_when_music_is_disabled(tmp_path: Path, repo_root: Path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "a.mp3").write_bytes(b"ID3")
    service = _service(tmp_path, repo_root, music_enabled=False)

    assert service.library()["items"] == []


def test_track_from_query_has_a_stable_id():
    first = track_from_query("告别前要跳舞", "汪苏泷", duration_ms=240_000)
    second = track_from_query("告别前要跳舞", "汪苏泷", duration_ms=240_000)
    other = track_from_query("不遗憾", "汪苏泷")

    assert first.id == second.id
    assert first.id != other.id
    assert first.title == "告别前要跳舞"
    assert other.duration_ms == 0


def test_lyrics_json_shape_is_the_midori_contract():
    outcome = LyricsOutcome(
        status="ready",
        source="lrc-file",
        lines=Lyrics(lines=(LyricLine(time_ms=1234, text="嗨"),), source="lrc-file").lines,
    )

    assert lyrics_json(outcome) == {
        "status": "ready",
        "source": "lrc-file",
        "lines": [{"timeMs": 1234, "text": "嗨"}],
        "karaokeLyric": "",
        "hasKaraokeLyric": False,
    }

    # 逐字通道：QQ/QRC 解出来的原始串**原样透传**（Task 10 的 Folia 引擎吃它），
    # hasKaraokeLyric 是它的布尔影子——两条恒存在，前端取字段不用做存在性判断。
    qrc = LyricsOutcome(
        status="ready",
        source="qq",
        lines=(LyricLine(time_ms=0, text="嗨呀"),),
        karaoke_lyric="[0,1000](0,500,0)嗨(500,500,0)呀",
    )
    payload = lyrics_json(qrc)
    assert payload["karaokeLyric"] == "[0,1000](0,500,0)嗨(500,500,0)呀"
    assert payload["hasKaraokeLyric"] is True
