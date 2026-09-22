"""常驻调度（PART 4 / TECH-DESIGN §10.3、§10.3.2）。

本包只做三件事：**幂等**（同一天/同一周只跑一次）、**兜底**（漏跑的能补上）、
**异常隔离**（job 炸了不能杀主进程）。任务本体（巩固 / 汇总 / 巡检）都在各自模块里，
这里只负责"什么时候跑、跑过没有、跑挂了怎么办"。

    >>> from yixiang.scheduler import JOBS, configure, run_job
"""

from __future__ import annotations

from yixiang.scheduler.jobs import (
    JOBS,
    JobContext,
    JobSpec,
    configure,
    context,
    ensure_runtime,
    idempotency_key,
    reset,
    run_job,
    spec_for,
)
from yixiang.scheduler.runtime import build_scheduler, install_jobs, serve

__all__ = [
    "JOBS",
    "JobContext",
    "JobSpec",
    "build_scheduler",
    "configure",
    "context",
    "ensure_runtime",
    "idempotency_key",
    "install_jobs",
    "reset",
    "run_job",
    "serve",
    "spec_for",
]
