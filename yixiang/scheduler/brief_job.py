"""晨报推送的**接口预留**（P2 才启用，TECH §10.3、§10.3.1）。

为什么现在写：晨报的**内容层**（``daily_brief``，按需触发）W4 之前就已经完整可用，
这一层只是"触发 + 补发 + 投递"。把接口和纯函数先摆在这儿，P2 动手时不用重写设计，
而且它**不参与任何门禁**——``brief_job`` 不在 ``yixiang.scheduler.JOBS`` 里，
``release_gate`` 也从不看它（PART-4 §2 的"P2 不进本部分门禁"）。

补发规则（§10.3.1，已实现为纯函数，可测）：

  * 今天已经成功发过（``scheduled_runs`` 里 ``brief`` + 今天 + ``ok``）→ 不发；
  * 否则，只要现在还没超过 ``YIXIANG_BRIEF_CATCHUP_UNTIL``（默认 12:00）→ 补发一次，
    文本前加 ``（补发）`` 标注；
  * 补发不造成重复推荐：已推去重以 ``recommend_log`` 为准，与触发源无关。
"""

from __future__ import annotations

from datetime import datetime, time

from yixiang.scheduler.jobs import JobSpec

BRIEF_JOB_NAME = "brief"
BRIEF_CRON = "0 8 * * *"
BRIEF_KEY_TMPL = "brief:{date}"
CATCHUP_LABEL = "（补发）"


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


async def deliver_brief() -> None:  # pragma: no cover - P2 才实现
    """P2：``cron`` 触发 → 组装 → 经 ``gateway/sinks.py`` 投递（cli / file / toast）。

    本阶段**只留接口**。故意抛 ``NotImplementedError`` 而不是静默 return：如果哪天
    有人把它挂进调度表，错误必须是响的。
    """
    raise NotImplementedError("晨报推送属 P2（PART-4 附录 A-1），本阶段未启用")


# 注意：**不在** ``yixiang.scheduler.JOBS`` 里——P2 启用时才追加进调度表
BRIEF_SPEC = JobSpec(
    name=BRIEF_JOB_NAME,
    cron=BRIEF_CRON,
    idempotency_key_tmpl=BRIEF_KEY_TMPL,
    fn=deliver_brief,
    summary="定时晨报推送 + 唤醒补发（P2，本阶段只预留接口）",
)


__all__ = [
    "BRIEF_CRON",
    "BRIEF_JOB_NAME",
    "BRIEF_KEY_TMPL",
    "BRIEF_SPEC",
    "CATCHUP_LABEL",
    "annotate_catchup",
    "brief_should_run_now",
    "deliver_brief",
    "parse_catchup_until",
]
