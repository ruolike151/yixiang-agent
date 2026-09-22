"""晨报推送：触发 + 补发 + 投递（TECH §10.3、§10.3.1）。

分工：**内容层**在 ``yixiang/tools/brief.py``（``daily_brief``，按需触发，不 import
调度器），这一层只管"什么时候发、发过没有、往哪发"。投递接在
``yixiang/gateway/sinks.py``（cli / file / toast），触发层由
``yixiang/scheduler/runtime.py`` 的 APScheduler 挂上 ``JOBS``。

补发规则（§10.3.1，已实现为纯函数，可测）：

  * 今天已经成功发过（``scheduled_runs`` 里 ``brief`` + 今天 + ``ok``）→ 不发；
  * 否则，只要现在还没超过 ``YIXIANG_BRIEF_CATCHUP_UNTIL``（默认 12:00）→ 补发一次，
    文本前加 ``（补发）`` 标注（准点那次：08:00 起 5 分钟内，不算补发）；
  * 补发不造成重复推荐：已推去重以 ``recommend_log`` 为准，与触发源无关。

三种"不发"（今天发过 / 过了窗口 / 推荐为空）都是 **ok**，不是 failed：把它们标红
只会让"失败可见"退化成噪音（与 ``job_consolidate`` 同一条纪律，§10.3.2）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time

from yixiang.scheduler.jobs import STATUS_OK, JobSpec, context

BRIEF_JOB_NAME = "brief"
BRIEF_CRON = "0 8 * * *"
BRIEF_KEY_TMPL = "brief:{date}"
CATCHUP_LABEL = "（补发）"
BRIEF_ON_TIME = "08:00"
CATCHUP_GRACE_MINUTES = 5


def parse_catchup_until(raw: str) -> time:
    """``"12:00"`` → ``time(12, 0)``；写坏了就退回默认（补发不该因为配置崩掉）。"""
    try:
        hour, _, minute = raw.partition(":")
        return time(int(hour), int(minute or 0))
    except ValueError:
        return time(12, 0)


def brief_should_run_now(
    now: datetime, *, catchup_until: str = "12:00", published_today: bool = False
) -> bool:
    """启动 / 每小时检查时调它：返 ``True`` 才发（§10.3.1 的算法）。"""
    if published_today:
        return False
    return now.time() <= parse_catchup_until(catchup_until)


def annotate_catchup(text: str, *, catchup: bool) -> str:
    """补发的晨报要标出来，否则用户会以为是"刚生成的"（§10.3.1 最后一条）。"""
    if not catchup:
        return text
    return f"{CATCHUP_LABEL}\n{text}"


def brief_is_catchup(
    now: datetime,
    *,
    on_time: str = BRIEF_ON_TIME,
    grace_minutes: int = CATCHUP_GRACE_MINUTES,
) -> bool:
    """准点那次（08:00 起 5 分钟内）不算补发，其余都标"（补发）"。

    没有这条界限，"8:00 发的"和"11:00 补的"在用户眼里一模一样，就分不清是当天的
    内容还是昨晚的残留（§10.3.1 最后一条）。
    """
    at = parse_catchup_until(on_time)
    deadline = at.hour * 60 + at.minute + grace_minutes
    return now.hour * 60 + now.minute > deadline


def published_today(conn: sqlite3.Connection, day: str) -> bool:
    """今天是否已经**成功**发过（``scheduled_runs`` 里 ``brief`` + 今天 + ``ok``）。"""
    row = conn.execute(
        "SELECT status FROM scheduled_runs WHERE job = ? AND run_date = ?",
        (BRIEF_JOB_NAME, day),
    ).fetchone()
    return bool(row) and str(row["status"]) == STATUS_OK


async def deliver_brief() -> str:
    """cron 触发（或启动补发）→ 组装 → 经 ``gateway/sinks.py`` 投递。

    返回给 ``scheduled_runs.detail`` 的一行人话。三种"不发"都不算失败：今天已经发
    过、已过补发窗口、推荐为空（内容层降级）——把它们标成 failed 只会让"失败可见"
    退化成噪音（与 ``job_consolidate`` 同一条纪律，§10.3.2）。
    """
    ctx = context()
    settings = ctx.settings
    now = ctx.clock.now()
    day = now.date().isoformat()
    if published_today(ctx.conn, day):
        return f"{day} 的晨报今天已经发过了"
    catchup_until = str(getattr(settings, "brief_catchup_until", "12:00"))
    if not brief_should_run_now(now, catchup_until=catchup_until):
        return f"已过补发窗口（{catchup_until}），{day} 不补发"

    from yixiang.gateway.sinks import deliver, parse_sinks
    from yixiang.tools.brief import daily_brief

    catchup = brief_is_catchup(now)
    body = annotate_catchup(
        daily_brief(ctx.conn, ctx.clock.now, ctx.data_dir), catchup=catchup
    )
    sinks = parse_sinks(str(getattr(settings, "brief_sink", "cli,file")))
    receipts = deliver(body, sinks=sinks, data_dir=ctx.data_dir, day=day)
    return f"{'补发' if catchup else '准点'}投递：{'；'.join(receipts)}"


# 触发层在 ``jobs.py`` 模块尾部 append 进 ``JOBS``（这里不 import 调度表，见那里的注释）
BRIEF_SPEC = JobSpec(
    name=BRIEF_JOB_NAME,
    cron=BRIEF_CRON,
    idempotency_key_tmpl=BRIEF_KEY_TMPL,
    fn=deliver_brief,
    summary="定时晨报推送 + 唤醒补发（cli / file / toast）",
)


__all__ = [
    "BRIEF_CRON",
    "BRIEF_JOB_NAME",
    "BRIEF_KEY_TMPL",
    "BRIEF_ON_TIME",
    "BRIEF_SPEC",
    "CATCHUP_GRACE_MINUTES",
    "CATCHUP_LABEL",
    "annotate_catchup",
    "brief_is_catchup",
    "brief_should_run_now",
    "deliver_brief",
    "parse_catchup_until",
    "published_today",
]
