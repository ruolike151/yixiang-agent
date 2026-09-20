"""事件循环纪律：一条线程一条常驻 loop，跨轮复用（TECH §10 的三个入口共用）。

这里锁的是一个真实现场里的 bug：``App`` 是长命对象，provider 缓存的
``httpx.AsyncClient`` 连接池绑在"建它的那条 loop"上。入口若每轮 ``asyncio.run()``
（新建 → 跑完 → 关掉），第二轮拿着旧连接池发请求就是 ``Event loop is closed``——
用户看到的现象是"第一句正常、第二句整轮失败"（而第一句是真的花了钱）。

所以用例不假装自己是 httpx，只钉最根本的那一条：**两轮必须跑在同一条 loop 上**。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
from typing import Any

import pytest
from conftest import make_settings
from fake_provider import FakeProvider, text_reply

from yixiang.app import App
from yixiang.ops.usage import JsonlUsageSink
from yixiang.runtime import eventloop
from yixiang.web import ConsoleAPI, build_server


class LoopRecorder:
    """记下"这一轮跑的哪条 loop"的假 Provider（其余全部转发给 ``FakeProvider``）。

    存的是 loop 对象本身而不是 ``id()``：id 会在旧 loop 被回收后复用，那样用例
    自己就会说谎。
    """

    def __init__(self, inner: FakeProvider) -> None:
        self.inner = inner
        self.loops: list[asyncio.AbstractEventLoop] = []
        self.close_loops: list[asyncio.AbstractEventLoop] = []

    async def complete(self, req: Any) -> Any:
        self.loops.append(asyncio.get_running_loop())
        return await self.inner.complete(req)

    async def complete_stream(self, req: Any, observer: Any = None) -> Any:
        self.loops.append(asyncio.get_running_loop())
        return await self.inner.complete_stream(req, observer)

    async def aclose(self) -> None:
        self.close_loops.append(asyncio.get_running_loop())
        await self.inner.aclose()


async def _current_loop() -> asyncio.AbstractEventLoop:
    return asyncio.get_running_loop()


def _chat_completion(text: str) -> bytes:
    """一个最小但合法的 chat completions 响应体（``_reply_from`` 认的字段都在）。"""
    return json.dumps(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _chat_completion_stream(text: str) -> bytes:
    """同一段回复的 SSE 版本（主对话走的就是流式这条路）。"""
    chunks = [
        {
            "id": "chatcmpl-1",
            "model": "deepseek-flash",
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}
            ],
        },
        {
            "id": "chatcmpl-1",
            "model": "deepseek-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        },
    ]
    lines = [f"data: {json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks]
    return ("\r\n\r\n".join([*lines, "data: [DONE]"]) + "\r\n\r\n").encode("utf-8")


def _completions_responder(text: str):
    """照 OpenAI 的口径回：要流式就回 SSE，不要就回 JSON。"""

    def respond(request: bytes) -> bytes:
        try:
            asked_stream = bool(json.loads(request).get("stream"))
        except ValueError:  # 不是 JSON 就当非流式，让用例在断言处炸出真原因
            asked_stream = False
        return _chat_completion_stream(text) if asked_stream else _chat_completion(text)

    return respond


@contextlib.contextmanager
def keepalive_endpoint(responder: Any):
    """只回 JSON、**保持长连接**的极简 HTTP/1.1 端点（真 httpx 的靶子）。

    ``Connection: keep-alive`` 是这条用例的关键：连接留在 httpx 的连接池里，下一轮
    才会去复用它——复用的那一刻就是 ``Event loop is closed``（如果 loop 换了）。
    产出 ``(endpoint, [(连接号, 请求体)])``——连接号让用例可以断言"第二句用的是
    第一句留下的那条连接"。
    """
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    hits: list[tuple[int, bytes]] = []
    accepted: list[int] = []
    stop = threading.Event()

    def handle(index: int, conn: socket.socket) -> None:
        with conn:
            buffer = b""
            while not stop.is_set():
                while b"\r\n\r\n" not in buffer:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                head, _, buffer = buffer.partition(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                while len(buffer) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                hits.append((index, buffer[:length]))
                buffer = buffer[length:]
                payload = responder(hits[-1][1])
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(payload)}\r\n".encode()
                    + b"Connection: keep-alive\r\n\r\n"
                    + payload
                )

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            accepted.append(len(accepted))
            threading.Thread(target=handle, args=(accepted[-1], conn), daemon=True).start()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", hits
    finally:
        stop.set()
        sock.close()
        thread.join(timeout=5)


def test_ask_runs_two_turns_on_the_same_event_loop(settings, clock):
    """终端入口（``yixiang``）：``App.ask()`` 连着跑两轮，不许中途换 loop。"""
    recorder = LoopRecorder(FakeProvider(text_reply("第一轮"), text_reply("第二轮")))
    app = App.from_settings(settings, provider=recorder, clock=clock)
    try:
        app.ask("你好")
        app.ask("再介绍一下你自己")
    finally:
        app.close()

    assert len(recorder.loops) == 2, "两轮各该问一次模型"
    assert recorder.loops[0] is recorder.loops[1], (
        "两轮跑在不同的 loop 上：第二轮复用 provider 里缓存的连接池就会 Event loop is closed"
    )


def test_console_chat_runs_two_turns_on_the_same_event_loop(settings, clock):
    """Web 入口：用户现场看到这个 bug 的那一层（第一句正常、第二句整轮失败）。"""
    sink = JsonlUsageSink(settings.usage_path, clock=clock)
    recorder = LoopRecorder(
        FakeProvider(
            text_reply("我是以湘。"),
            text_reply("《夏日重现》是一部……"),
            usage_sink=sink,
            clock=clock,
            stream_pieces=4,
        )
    )

    def factory(current):
        return App.from_settings(current, provider=recorder, usage_sink=sink, clock=clock)

    api = ConsoleAPI(settings, app_factory=factory)
    try:
        first = api.chat("你好 介绍一下你自己")
        second = api.chat("帮我查一下夏日重现 介绍这部番剧")
    finally:
        api.close()

    assert first["error"] is None and second["error"] is None
    assert len(recorder.loops) == 2, "两轮各该问一次模型"
    assert recorder.loops[0] is recorder.loops[1], (
        "两轮跑在不同的 loop 上：第二轮拿旧连接池发请求就是 Event loop is closed"
    )


def test_close_closes_the_provider_on_the_same_loop(settings, clock):
    """收尾也得同一条 loop：否则 httpx 客户端关不掉，连接池吊在死 loop 上。"""
    recorder = LoopRecorder(FakeProvider(text_reply("好")))
    app = App.from_settings(settings, provider=recorder, clock=clock)
    try:
        app.ask("你好")
    finally:
        app.close()

    assert len(recorder.close_loops) == 1, "close() 该关一次 provider"
    assert recorder.close_loops[0] is recorder.loops[0], (
        "provider 是在另一条 loop 上关的：连接池关不掉"
    )


def test_run_inside_a_running_loop_says_so():
    """在事件循环里同步跑会套娃两层 loop：这里要拦住，并把做法说清楚。"""

    async def nested() -> None:
        with pytest.raises(RuntimeError, match="不能"):
            eventloop.run(asyncio.sleep(0))

    asyncio.run(nested())


def test_shutdown_closes_the_loop_and_the_next_run_starts_a_fresh_one():
    """线程收尾：关掉常驻 loop，之后再跑一轮能自己重建（且幂等）。"""
    first = eventloop.run(_current_loop())
    eventloop.shutdown()
    assert first.is_closed()

    eventloop.shutdown()  # 幂等：没有 loop 时不该炸

    second = eventloop.run(_current_loop())
    assert second is not first and not second.is_closed()
    eventloop.shutdown()


def test_shutdown_cancels_leftover_tasks_before_closing():
    """常驻 loop 上还挂着后台任务时，收尾要取消掉再关（否则退出时一串告警）。"""
    loop = eventloop.run(_current_loop())
    leftover = loop.create_task(asyncio.sleep(30))

    eventloop.shutdown()

    assert leftover.cancelled()
    assert loop.is_closed()


def test_web_worker_thread_closes_its_loop_when_it_stops(settings, clock):
    """Web 的工作线程收工时关掉自己的常驻 loop（谁建谁关）。"""
    sink = JsonlUsageSink(settings.usage_path, clock=clock)
    recorder = LoopRecorder(FakeProvider(text_reply("好"), usage_sink=sink, clock=clock))

    def factory(current):
        return App.from_settings(current, provider=recorder, usage_sink=sink, clock=clock)

    api = ConsoleAPI(settings, app_factory=factory)
    server = build_server(settings, port=0, api=api, quiet=True)
    seen: list[asyncio.AbstractEventLoop] = []
    try:
        # 让工作线程把"自己的 loop"交出来，收工后再看它有没有被关掉
        server.runner.call(lambda: seen.append(eventloop.run(_current_loop())))
    finally:
        server.close_all()

    assert len(seen) == 1
    assert seen[0].is_closed(), "工作线程退出时没关自己的 loop"


def test_two_console_turns_reuse_one_keepalive_connection(tmp_path, repo_root, clock):
    """用户现场的最小复现：**真 httpx + 真连接池**，连着两轮打同一个长连接端点。

    假 Provider 复现不出这个 bug（它没有连接池），所以这条用例用的是真的
    ``OpenAICompatibleProvider``——只是把 ``api_base`` 指到本机的假端点，依然离线、
    零成本、不碰真模型（§13.6）。
    """
    responder = _completions_responder("我在。")
    with keepalive_endpoint(responder) as (endpoint, hits):
        settings = make_settings(tmp_path, repo_root, api_base=endpoint)
        api = ConsoleAPI(settings)
        try:
            first = api.chat("你好 介绍一下你自己")
            after_first = len(hits)  # 这一轮到底发了几次请求由 App 决定（门控可能插一手）
            second = api.chat("帮我查一下夏日重现 介绍这部番剧")
        finally:
            api.close()

    assert first["error"] is None and second["error"] is None, (first, second)
    assert first["reply"] == second["reply"] == "我在。"
    assert after_first >= 1 and len(hits) > after_first, "第二句一次请求都没发出去"
    # 门控会另起一个 provider（自己的连接池），所以这里不比连接总数，只问一句：
    # 第二句有没有用上第一句留下的连接？——换 loop 时，答案会变成"用不上"，而
    # 那一刻就是用户看到的 Event loop is closed
    first_round = {index for index, _ in hits[:after_first]}
    second_round = {index for index, _ in hits[after_first:]}
    assert first_round & second_round, "第二句没复用第一句的连接：连接池没有跨轮存活"
