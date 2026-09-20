"""抓取路径：用假 client 把"限速 / 退避 / 缓存 / 游标"钉死（TECH §8.1）。

这一组**不出网**：``httpx.MockTransport`` 在进程内回剧本，但走的是
``fetch_bangumi`` / ``fetch_tmdb`` 的真实代码路径——包括 ``_cached_json`` 落盘、
游标写 ``meta``、以及每一次 ``sleep`` 的具体秒数。

为什么逐个记 ``sleep``：限速是我们对第三方接口的礼貌，不是实现细节。
把它变成可断言的数字，Task 7 真出网时才只剩"数据对不对"一个问题。
"""

from __future__ import annotations

import json

import httpx
import pytest

from yixiang import db
from yixiang.rag import ingest


class SyncSleep:
    """同步 sleep 记录器。

    ``ingest`` 的 ``sleep`` 参数是**同步**可调用对象（``time.sleep`` 的形状），
    所以 ``evals/deterministic/fake_provider.py`` 里那个 async 版的
    ``SleepRecorder`` 在这里用不了——传进去只会得到一个没被 await 的协程。
    """

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(round(seconds, 6))


def _bangumi_page(*ids: int) -> dict:
    """一页 Bangumi 响应（字段名与 ``/v0/search/subjects`` 的 ``data`` 一致）。"""
    return {
        "data": [
            {
                "id": subject_id,
                "name": f"Subject {subject_id}",
                "name_cn": f"条目 {subject_id}",
                "platform": "TV",
                "rating": {"score": 7.5},
                "tags": [{"name": "动画"}],
                "date": f"2023-01-0{index + 1}",
                "summary": "一句简介",
                "images": {"common": "https://example.test/c.jpg"},
            }
            for index, subject_id in enumerate(ids)
        ]
    }


def _client(handler) -> httpx.Client:
    """把剧本塞进真 ``httpx.Client``：``_request_json`` 拿到的是它平时的那个对象。

    注意 ``_request_json`` 里 ``owned = client is None``——我们自己传进来的 client
    归我们自己关（用例里用 try/finally 关掉），不会被框架顺手 close。
    """
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=1.0)


def test_a_transient_failure_is_retried_with_the_real_backoff(tmp_path):
    """前两次断连、第三次成功：睡眠序列是 1s / 1s / 1s / 2s。"""
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(1)
        if len(seen) < 3:
            raise httpx.ConnectError("模拟断连", request=request)
        return httpx.Response(200, json=_bangumi_page(1))

    sleep = SyncSleep()
    client = _client(handler)
    try:
        items = ingest.fetch_bangumi(data_dir=tmp_path, client=client, sleep=sleep)
    finally:
        client.close()

    assert [item.source_id for item in items] == ["bangumi:1"]
    assert len(seen) == 3
    # 失败后固定 REQUEST_INTERVAL_S，重试前再按 attempt × REQUEST_INTERVAL_S 递增
    assert sleep.calls == [1.0, 1.0, 1.0, 2.0]


def test_all_retries_failing_raises_and_leaves_no_half_written_cache(tmp_path):
    """三次都失败：抛 RuntimeError，并且 ``data/raw/`` 一个文件都不许留。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "boom"})

    sleep = SyncSleep()
    client = _client(handler)
    try:
        with pytest.raises(RuntimeError) as info:
            ingest.fetch_bangumi(data_dir=tmp_path, client=client, sleep=sleep)
    finally:
        client.close()

    assert "抓取失败" in str(info.value)
    assert sleep.calls == [1.0, 1.0, 1.0, 2.0, 1.0]
    assert not (tmp_path / ingest.RAW_DIRNAME).exists()


def test_two_pages_advance_the_cursor_and_cache_every_page(tmp_path, conn):
    """``pages=2``：两次请求、游标落在 ``meta``、每页一份原始 JSON 缓存。"""
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        offsets.append(body["offset"])
        start = body["offset"] + 1
        return httpx.Response(200, json=_bangumi_page(start, start + 1))

    client = _client(handler)
    try:
        items = ingest.fetch_bangumi(
            data_dir=tmp_path,
            conn=conn,
            pages=2,
            client=client,
            sleep=SyncSleep(),
        )
    finally:
        client.close()

    assert offsets == [0, 20]
    # id 跟着请求的 offset 走：第二页（offset=20）拿到 21/22，
    # 顺带证明第二页没有重复返回第一页的数据。
    assert [item.source_id for item in items] == [
        "bangumi:1",
        "bangumi:2",
        "bangumi:21",
        "bangumi:22",
    ]
    assert db.get_meta(conn, ingest.CURSOR_META.format(source="bangumi")) == "40"
    names = sorted(path.name for path in (tmp_path / ingest.RAW_DIRNAME).iterdir())
    assert names == ["bangumi-all-all-0.json", "bangumi-all-all-20.json"]


def test_resume_continues_from_the_stored_cursor(tmp_path, conn):
    """``resume=True`` 接着游标跑：第三次请求的 offset 是 40，缓存文件名也接上。"""
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        offsets.append(body["offset"])
        return httpx.Response(200, json=_bangumi_page(body["offset"] + 1))

    first = _client(handler)
    try:
        ingest.fetch_bangumi(
            data_dir=tmp_path,
            conn=conn,
            pages=2,
            client=first,
            sleep=SyncSleep(),
        )
    finally:
        first.close()

    second = _client(handler)
    try:
        ingest.fetch_bangumi(
            data_dir=tmp_path,
            conn=conn,
            pages=1,
            resume=True,
            client=second,
            sleep=SyncSleep(),
        )
    finally:
        second.close()

    assert offsets == [0, 20, 40]
    assert db.get_meta(conn, ingest.CURSOR_META.format(source="bangumi")) == "60"


def test_a_second_run_hits_the_cache_and_never_touches_the_client(tmp_path):
    """重跑不重抓：第二次给的 client 一碰就炸，流程照样出结果。"""

    class ExplodingClient:
        def post(self, *args, **kwargs):
            raise AssertionError("命中缓存时不该再发请求")

        def get(self, *args, **kwargs):
            raise AssertionError("命中缓存时不该再发请求")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_bangumi_page(7))

    first_client = _client(handler)
    try:
        first = ingest.fetch_bangumi(
            data_dir=tmp_path, client=first_client, sleep=SyncSleep()
        )
    finally:
        first_client.close()

    second = ingest.fetch_bangumi(
        data_dir=tmp_path, client=ExplodingClient(), sleep=SyncSleep()
    )

    assert [item.source_id for item in first] == ["bangumi:7"]
    assert [item.source_id for item in second] == ["bangumi:7"]


def test_tmdb_goes_through_get_and_reads_the_results_key(tmp_path):
    """TMDb 是 GET + ``results``（Bangumi 是 POST + ``data``），别把两条路径搞混。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.params.get('page')}")
        return httpx.Response(200, json={"results": [{"id": 9, "title": "沙丘"}]})

    client = _client(handler)
    try:
        items = ingest.fetch_tmdb(
            data_dir=tmp_path,
            api_key="fake-key",
            client=client,
            sleep=SyncSleep(),
        )
    finally:
        client.close()

    assert seen == ["GET 1"]
    assert [item.source_id for item in items] == ["tmdb:9"]


def test_the_raw_cache_is_utf8_json_so_a_human_can_read_it(tmp_path):
    """缓存是给人排查用的：UTF-8 + 中文不转义（``relaunch`` 看原始响应时省一次解码）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_bangumi_page(1))

    client = _client(handler)
    try:
        ingest.fetch_bangumi(data_dir=tmp_path, client=client, sleep=SyncSleep())
    finally:
        client.close()

    raw = (tmp_path / ingest.RAW_DIRNAME / "bangumi-all-all-0.json").read_bytes()

    # 显式写编码：这条断言的字面语义就是"落盘字节是 UTF-8"（UP012 认为默认值可省，
    # 但省掉之后断言的意图就只剩"能编成字节"，所以这里保留参数）
    assert "条目 1".encode("utf-8") in raw  # noqa: UP012
    assert b"\\u6761" not in raw  # ensure_ascii=False
