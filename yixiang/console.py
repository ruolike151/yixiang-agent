"""终端输出的编码统一（TECH §18.1 的落地细节，Windows 专属坑）。

Windows 控制台默认用 GBK，而这一层到处在打印 ``✓ / ⚠ / ✗ / 《》`` 与中文标题。
一旦被重定向（CI、``> out.txt``、父进程捕获），GBK 编不出来就直接抛
``UnicodeEncodeError``——**演示当天才发现就晚了**。

所以只暴露一个函数：把 stdout / stderr 重配成 UTF-8，失败就保持原样（已经有别的
库接管输出流时不硬抢）。所有会打印非 ASCII 的入口都该先调它一次。
"""

from __future__ import annotations

import sys
from typing import Any


def force_utf8_stdio(streams: tuple[Any, ...] | None = None) -> None:
    """尽力把给定输出流切成 UTF-8；任何失败都静默跳过（渲染质量不值得崩进程）。"""
    for stream in streams or (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # 已被别的库接管 / 不可重配：保持原样
            continue


__all__ = ["force_utf8_stdio"]
