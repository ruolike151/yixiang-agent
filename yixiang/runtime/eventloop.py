"""常驻事件循环：一条线程一条 loop，跨轮复用（CLI / Web / 脚本共用一条规矩）。

为什么不能每轮 ``asyncio.run()``：``App`` 是长命对象，provider 缓存的
``httpx.AsyncClient`` 连接池绑在"建它的那条 loop"上。``asyncio.run`` 跑完就把 loop
关掉，第二轮复用那条连接池就是 ``RuntimeError: Event loop is closed``——现场表现
正是"第一句正常、第二句整轮失败"（而第一句已经真实计费）。

所以入口统一走这里：loop 与线程同寿，与它上面缓存的异步资源（连接池、锁、任务）
同生共死。谁建谁用——``sqlite3`` 连接的纪律一样，App 也不跨线程。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Coroutine
from typing import Any

# 按线程存 loop：Web 的工作线程、CLI 的主线程各有一条，互不干扰
_state = threading.local()


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """取本线程的常驻 loop；没有（或已被关掉）就新建一条。"""
    loop = getattr(_state, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _state.loop = loop
    # 别人可能用 asyncio.run() 把当前 loop 设置清掉了（例如 doctor 的探活），
    # 这里每次重申一次，省得 get_event_loop() 的调用方拿到空的
    asyncio.set_event_loop(loop)
    return loop


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """在本线程的常驻 loop 上同步跑完一个协程。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # 已经在事件循环里：再 run_until_complete 就是套娃两层 loop（asyncio 会拦，
        # 但"another loop is running"这句话指不到病根）。把话说明白，顺手收掉协程，
        # 免得留下 "coroutine was never awaited" 的告警
        coro.close()
        raise RuntimeError("不能在事件循环里同步跑：这里该直接 await 这个协程")
    return _ensure_loop().run_until_complete(coro)


def shutdown() -> None:
    """收尾：取消本线程 loop 上的残留任务并关掉它（幂等；谁建谁关）。

    线程要结束的时候调用它（Web 的工作线程就是这么做的）。``App.close()`` **不**
    调用它——loop 是线程级的，同一个线程上完全可能有不止一个 App。
    """
    loop = getattr(_state, "loop", None)
    if loop is None:
        return
    _state.loop = None
    with contextlib.suppress(Exception):  # 收尾阶段不往外抛，但照样往下关
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    with contextlib.suppress(Exception):
        loop.close()
    # 别把一条已关闭的 loop 留在"当前 loop"的位置上，免得别人 get_event_loop() 拿到它
    with contextlib.suppress(Exception):
        asyncio.set_event_loop(None)
