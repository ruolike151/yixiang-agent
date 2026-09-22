"""``yixiang serve`` 的接线：APScheduler 挂 JOBS + 两个开关的语义（TECH §10.2.1/§10.3）。"""

from __future__ import annotations

import asyncio

from yixiang.scheduler.jobs import JOBS
from yixiang.scheduler.runtime import build_scheduler, install_jobs, serve


def test_every_job_lands_on_the_scheduler_with_its_cron():
    """挂上去的 id 与 cron 必须一一对应（'八个 job 只挂七个'这种漏要在用例里现形）。"""
    scheduler = build_scheduler()

    ids = install_jobs(scheduler)

    assert ids == [spec.name for spec in JOBS]
    assert sorted(job.id for job in scheduler.get_jobs()) == sorted(ids)
    # cron 是冻结契约（PART-4 §4）：只抽两条最容易写错的做字面量断言
    assert (
        str(scheduler.get_job("brief").trigger)
        == "cron[month='*', day='*', day_of_week='*', hour='8', minute='0']"
    )
    assert (
        str(scheduler.get_job("verify").trigger)
        == "cron[month='*', day='*', day_of_week='0', hour='22', minute='0']"
    )


def test_serve_refuses_to_spin_when_nothing_is_enabled(settings, capsys):
    """两个开关都没开：说清楚该改哪个键，退 2——别安静地空转。"""
    code = asyncio.run(serve(settings))

    assert code == 2
    assert "YIXIANG_SCHEDULER_ENABLED" in capsys.readouterr().out


def test_serve_reports_configuration_problems_before_starting(settings, capsys):
    """QQ 开了但没白名单：启动前就报错（§10.2.5-1），不会起来再被陌生人敲门。"""
    settings.qq_enabled = True
    settings.qq_allowed = ""

    code = asyncio.run(serve(settings))

    assert code == 1
    assert "YIXIANG_QQ_ALLOWED" in capsys.readouterr().out
