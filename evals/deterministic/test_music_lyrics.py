"""音乐：歌词模型、LRC 解析、本地 .lrc 匹配（Task 1）+ LRCLib 缓存（Task 2）。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from yixiang.media.lrclib import (
    LRCLIB_ENDPOINT,
    LrclibUnavailable,
    cache_path,
    fetch_lrclib,
    read_cache,
    write_cache,
)
from yixiang.media.lyrics import (
    LyricLine,
    Lyrics,
    Track,
    find_local_lyrics,
    load_local_lyrics,
    parse_lrc,
)


def test_parse_lrc_reads_centisecond_and_millisecond_stamps():
    lines = parse_lrc("[00:12.34]第一行\n[00:15.345]第二行\n")

    assert [(line.time_ms, line.text) for line in lines] == [
        (12340, "第一行"),
        (15345, "第二行"),
    ]


def test_parse_lrc_sorts_and_expands_repeated_timestamps():
    lines = parse_lrc("[00:05.00]副歌\n[00:01.00][00:03.00]主歌\n")

    assert [(line.time_ms, line.text) for line in lines] == [
        (1000, "主歌"),
        (3000, "主歌"),
        (5000, "副歌"),
    ]


def test_offset_tag_shifts_every_line_and_metadata_tags_are_ignored():
    lines = parse_lrc("[ar:汪苏泷]\n[ti:告别前要跳舞]\n[offset:-500]\n[00:10.00]你走以后\n")

    assert [(line.time_ms, line.text) for line in lines] == [(9500, "你走以后")]


def test_blank_and_untimed_lines_are_dropped():
    lines = parse_lrc("没有时间戳的一行\n[00:01.00]\n[00:02.00]   \n[00:03.00]真的歌词\n")

    assert [(line.time_ms, line.text) for line in lines] == [(3000, "真的歌词")]


def test_find_local_lyrics_prefers_an_exact_normalised_match(tmp_path: Path):
    (tmp_path / "汪苏泷 - 告别前要跳舞.lrc").write_text("[00:01.00]x\n", encoding="utf-8")
    (tmp_path / "告别前要跳舞.lrc").write_text("[00:01.00]y\n", encoding="utf-8")
    track = Track(id="local/告别前要跳舞.mp3", title="告别前要跳舞", artist="汪苏泷")

    found = find_local_lyrics(track, tmp_path)

    assert found is not None
    assert found.name == "汪苏泷 - 告别前要跳舞.lrc"


def test_find_local_lyrics_falls_back_to_a_containing_filename(tmp_path: Path):
    (tmp_path / "01 告别前要跳舞 (Live).lrc").write_text("[00:01.00]x\n", encoding="utf-8")
    track = Track(id="x", title="告别前要跳舞", artist="")

    assert find_local_lyrics(track, tmp_path).name == "01 告别前要跳舞 (Live).lrc"


def test_find_local_lyrics_returns_none_when_the_dir_is_missing(tmp_path: Path):
    assert find_local_lyrics(Track(id="x", title="y"), tmp_path / "nope") is None


def test_load_local_lyrics_falls_back_to_gbk(tmp_path: Path):
    (tmp_path / "t.lrc").write_bytes("[00:01.00]中文\n".encode("gbk"))
    loaded = load_local_lyrics(Track(id="t.mp3", title="t"), tmp_path)

    assert loaded is not None
    assert loaded.source == "lrc-file"
    assert loaded.lines[0].text == "中文"
    assert loaded.word_by_word is False


def test_a_lyrics_object_with_no_lines_is_empty():
    assert Lyrics(lines=(), source="none").is_empty() is True
    assert Lyrics(lines=(LyricLine(time_ms=0, text="x"),), source="lrc-file").is_empty() is False


# --------------------------------------------------------------- Task 2 起
def _synced(*lines: str) -> dict[str, object]:
    return {"syncedLyrics": "\n".join(lines) + "\n", "plainLyrics": "没有时间轴"}


def _client(payload: object, *, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_lrclib_parses_synced_lyrics_and_sends_second_precision_duration():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/get"
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=_synced("[00:12.34]第一行", "[00:15.345]第二行"))

    track = Track(
        id="x",
        title="告别前要跳舞",
        artist="汪苏泷",
        album="21世纪罗曼史",
        duration_ms=245_400,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))

    lyrics = fetch_lrclib(track, client=client)

    assert LRCLIB_ENDPOINT.endswith("/api/get")
    assert seen == {
        "track_name": "告别前要跳舞",
        "artist_name": "汪苏泷",
        "album_name": "21世纪罗曼史",
        "duration": "245",  # 245_400 ms → round → 245 秒
    }
    assert lyrics is not None
    assert lyrics.source == "lrclib"
    assert lyrics.word_by_word is False
    assert [(line.time_ms, line.text) for line in lyrics.lines] == [
        (12340, "第一行"),
        (15345, "第二行"),
    ]


def test_fetch_lrclib_omits_artist_album_and_duration_when_unknown():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=_synced("[00:01.00]x"))

    client = httpx.Client(transport=httpx.MockTransport(handler))

    fetch_lrclib(Track(id="x", title="只有歌名"), client=client)

    assert seen == {"track_name": "只有歌名"}


def test_fetch_lrclib_returns_none_when_only_plain_lyrics_exist():
    payload = {"syncedLyrics": None, "plainLyrics": "只有没有时间轴的歌词"}

    assert fetch_lrclib(Track(id="x", title="y"), client=_client(payload)) is None


def test_fetch_lrclib_returns_none_on_404():
    payload = {"message": "not found"}

    assert fetch_lrclib(Track(id="x", title="y"), client=_client(payload, status=404)) is None


def test_fetch_lrclib_raises_unavailable_on_server_error():
    with pytest.raises(LrclibUnavailable):
        fetch_lrclib(Track(id="x", title="y"), client=_client({"message": "boom"}, status=502))


def test_fetch_lrclib_raises_unavailable_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    with pytest.raises(LrclibUnavailable):
        fetch_lrclib(Track(id="x", title="y"), client=client)


def test_cache_roundtrip_and_read_cache_is_none_on_missing_or_broken_files(tmp_path: Path):
    track = Track(id="x", title="t", artist="a", album="b", duration_ms=1000)
    other = Track(id="x", title="t", artist="a", album="b", duration_ms=2000)
    path = cache_path(tmp_path, track)

    assert path.parent == tmp_path
    assert path.suffix == ".json"
    assert cache_path(tmp_path, other) != path, "时长不同 = 两首歌，缓存键必须分开"
    assert read_cache(path) is None

    write_cache(path, Lyrics(lines=(LyricLine(time_ms=1000, text="中文"),), source="lrclib"))
    loaded = read_cache(path)

    assert loaded is not None
    assert loaded.source == "lrclib"
    assert [(line.time_ms, line.text) for line in loaded.lines] == [(1000, "中文")]

    path.write_text("{ 这不是 json", encoding="utf-8")
    assert read_cache(path) is None
