"""Agent Loop：reason → act → observe + 三条循环防护。"""

from yixiang.loop.agent import ITER_LIMIT_NOTICE, TOOL_ROUND_NOTICE, run_loop
from yixiang.loop.guard import DUPLICATE_NOTICE, DUPLICATE_STOP, Guard, GuardHit

__all__ = [
    "DUPLICATE_NOTICE",
    "DUPLICATE_STOP",
    "Guard",
    "GuardHit",
    "ITER_LIMIT_NOTICE",
    "TOOL_ROUND_NOTICE",
    "run_loop",
]
