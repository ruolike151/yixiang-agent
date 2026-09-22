"""``yixiang serve`` 的骨架：APScheduler 挂 ``JOBS``，和 QQ 网关共用一条常驻 loop。

为什么触发层在这里而不在 ``jobs.py``：``jobs.py`` 是**纯业务**（幂等键、异常隔离、
任务本体），它到今天为止一次都没 import 过 apscheduler。把调度库隔离在这个文件里，
业务用例就永远不需要装调度器跑起来（``test_scheduler.py`` 至今是零依赖的）。

补发语义不靠调度库：``misfire_grace_time=None`` 只保证"睡醒后照样触发一次"，
真正的"该不该发、发过没有"由 job 内部判定（§10.3.1 + ``scheduled_runs`` 幂等）。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from yixiang.config import Settings
from yixiang.gateway.qq import (
    PROCESSED_RETENTION_DAYS,
    QQGateway,
    parse_listen,
    prune_processed,
)
from yixiang.scheduler.jobs import JOBS, JobSpec, run_job

DEFAULT_TIMEZONE = "Asia/Shanghai"


def build_scheduler(*, timezone: str = DEFAULT_TIMEZONE) -> AsyncIOScheduler:
    """进程内调度器；``AsyncIOScheduler`` 直接跑在入口那条常驻 loop 上。"""
    return AsyncIOScheduler(timezone=timezone)


def install_jobs(
    scheduler: AsyncIOScheduler,
    specs: Iterable[JobSpec] = JOBS,
    *,
    runner: Callable[[str], Awaitable[None]] = run_job,
) -> list[str]:
    """按 ``spec.cron`` 把任务挂上，返回挂好的 job id（顺序 = 传入顺序）。

    ``coalesce=True`` 把挤在一起的多个错过的触发点合并成一次；``misfire_grace_time=None``
    表示"迟到了也跑"——幂等由 job 自己守，调度库不需要知道业务规则。
    """
    ids: list[str] = []
    for spec in specs:
        scheduler.add_job(
            runner,
            trigger=CronTrigger.from_crontab(spec.cron, timezone=scheduler.timezone),
            args=[spec.name],
            id=spec.name,
            name=spec.summary or spec.name,
            coalesce=True,
            misfire_grace_time=None,
        )
        ids.append(spec.name)
    return ids


async def _idle() -> None:
    """只跑调度器时的挂起：等 Ctrl+C（取消由入口的 KeyboardInterrupt 接住）。"""
    await asyncio.Event().wait()


async def serve(
    settings: Settings,
    *,
    host: str | None = None,
    port: int | None = None,
    qq: bool | None = None,
    scheduler: bool | None = None,
) -> int:
    """常驻：把 QQ 网关与调度器一起开起来（谁没配就跳过谁）。

    两个开关都没开 = 没有什么可以常驻的：直接说清楚该改哪个键，而不是安静空转。
    """
    want_qq = settings.qq_enabled if qq is None else qq
    want_scheduler = settings.scheduler_enabled if scheduler is None else scheduler
    if not want_qq and not want_scheduler:
        print(
            "QQ 与调度都没开：设 YIXIANG_QQ_ENABLED=1 或 YIXIANG_SCHEDULER_ENABLED=1 再来。"
        )
        return 2
    problems = settings.validate()
    if problems:
        for line in problems:
            print(f"× {line}")
        return 1

    from yixiang import db
    from yixiang.app import App
    from yixiang.scheduler.jobs import configure, context, ensure_runtime

    conn = db.connect(settings.db_path)
    db.migrate(conn)
    app = App.from_settings(settings, conn=conn)
    sched = build_scheduler()
    gateway: QQGateway | None = None
    try:
        if want_scheduler:
            configure(
                conn,
                data_dir=settings.data_dir,
                clock=app.clock,
                settings=settings,
                provider=app.provider,
            )
            install_jobs(sched)
            ensure_runtime(context())
            sched.start()
            print(f"调度已启动：{', '.join(spec.name for spec in JOBS)}")
        if want_qq:
            listen = settings.qq_listen
            if host or port:
                base_host, base_port = parse_listen(listen)
                listen = f"{host or base_host}:{port or base_port}"
            gateway = QQGateway(app, settings=settings, conn=conn, listen=listen)
            removed = prune_processed(
                conn,
                before=app.clock.now() - timedelta(days=PROCESSED_RETENTION_DAYS),
            )
            print(f"QQ 网关监听 ws://{listen}/onebot/v11/ws（清理 {removed} 条旧幂等记录）")
        # 这里是**协程内部**，直接 await：loop 由入口（``cmd_serve``）建、也由它关。
        # 在协程里再套一次 ``eventloop.run`` 会撞上"不能在事件循环里同步跑"——那条错
        # 的现场表现正是"命令打印了一行 coroutine 对象然后退 1"（谁建谁关，§10.1）。
        try:
            if gateway is not None:
                await gateway.serve_forever()
            else:
                await _idle()
        except KeyboardInterrupt:
            print("\n收工。")
        return 0
    finally:
        with contextlib.suppress(Exception):
            sched.shutdown(wait=False)
        with contextlib.suppress(Exception):
            app.close()
