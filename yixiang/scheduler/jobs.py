"""三个常驻 job 的幂等、补发与异常隔离（TECH §10.3、§10.3.2、PART-4 §3/§5-7）。

调度表（时间是本地时间，cron 是五段式）：

| job | cron | 幂等键 | 说明 |
|---|---|---|---|
| ``consolidate`` | ``30 23 * * *`` | 水印（§7.7.1） | 巩固兜底：当天没凑够 ``consolidate_every`` 轮也要蒸馏一次 |
| ``usage`` | ``50 23 * * *`` | ``usage:YYYY-MM-DD`` | 每日汇总写 ``reports/usage-YYYY-MM-DD.json`` |
| ``verify`` | ``0 22 * * 0`` | ``verify:YYYY-Www`` | 周日记忆巡检（``memory verify`` 对账） |

三条纪律（改之前先读）：

  1. **幂等靠 ``scheduled_runs``**：``UNIQUE(job, run_date)`` 是唯一事实来源。已经
     ``ok`` 的当天任务再触发一次 = 直接跳过，不是"再跑一遍碰运气"。
  2. **失败的 job 不占位**：只有 ``ok`` 会挡住重试，``failed`` 留着让下一次补上
     （这正是 PART-4 §7 的 D-27 补发语义）。
  3. **异常绝不外抛**（§10.3.2）：``run_job`` 捕获一切 ``Exception``，写一行
     ``data/logs/jobs-YYYY-MM-DD.jsonl`` 留痕，然后正常返回。巩固失败绝不能
     让用户发不出消息。

任务的返回值约定：**返一个字符串就是这次的 detail**（写进 ``scheduled_runs``
与 job 日志），返 ``None`` 表示"没什么好说的"。这样"跑了、成了、为什么"三件事
在同一行里说得清，而不用把日志打印耦合进任务本身。

为什么 job 日志不写进 ``data/traces/``：trace 的契约是"一行一轮对话"，``ops tail`` /
``show-trace`` / ``--explain`` 都按这个假设读它；塞进 job 记录会把每行都变成需要
分支判断的联合类型。日志是日志，trace 是 trace。
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from yixiang.runtime.models import Clock, SystemClock, to_local_iso

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

JOB_LOG_DIRNAME = "logs"
REPORTS_DIRNAME = "reports"


@dataclass(slots=True)
class JobSpec:
    """一个定时任务：``cron`` 是给人看的排期，``idempotency_key_tmpl`` 是去重依据。

    ``idempotency_key_tmpl`` 为空 = **不做 ``scheduled_runs`` 去重**，幂等由业务自己
    保证（巩固就是这个情况：它的水印 §7.7.1 比"今天跑过没有"更准）。
    """

    name: str
    cron: str
    idempotency_key_tmpl: str
    fn: Callable[[], Awaitable[Any]]
    summary: str = ""

    @property
    def deduped(self) -> bool:
        return bool(self.idempotency_key_tmpl)


@dataclass(slots=True)
class JobContext:
    """组装根注入的运行时：连接、目录、时钟、模型。"""

    conn: Any = None
    data_dir: Path = field(default_factory=lambda: Path("data"))
    clock: Clock = field(default_factory=SystemClock)
    settings: Any = None
    provider: Any = None


_context: JobContext | None = None


def configure(
    conn: Any = None,
    *,
    data_dir: Path | str = "data",
    clock: Clock | None = None,
    settings: Any = None,
    provider: Any = None,
) -> JobContext:
    """装配调度上下文（一次 configure，``run_job`` 全局只读）。"""
    global _context
    _context = JobContext(
        conn=conn,
        data_dir=Path(data_dir),
        clock=clock or SystemClock(),
        settings=settings,
        provider=provider,
    )
    return _context


def context() -> JobContext:
    if _context is None:
        raise RuntimeError(
            "调度子系统尚未装配：先调用 yixiang.scheduler.configure(conn, data_dir=...)"
        )
    return _context


def reset() -> None:
    """测试用：清掉全局上下文（避免用例之间串库 / 串时钟）。"""
    global _context
    _context = None


# ------------------------------------------------------------------ 幂等键
def spec_for(name: str) -> JobSpec | None:
    return next((spec for spec in JOBS if spec.name == name), None)


def idempotency_key(spec: JobSpec | None, now: datetime) -> str:
    """``usage:{date}`` / ``verify:{week}``（PART-4 §4 的冻结键名）。

    空模板返回空串 = 不去重（巩固的水印比日历更准）。
    """
    if spec is None or not spec.idempotency_key_tmpl:
        return ""
    iso = now.isocalendar()
    return spec.idempotency_key_tmpl.format(
        date=now.date().isoformat(), week=f"{iso.year}-W{iso.week:02d}"
    )


def _split_key(key: str) -> tuple[str, str]:
    """``usage:2026-09-19`` → ``("usage", "2026-09-19")``；没冒号就是纯日期。"""
    job, sep, run_date = key.partition(":")
    return (job, run_date) if sep else (key, "")


def _already_ok(conn: Any, job: str, run_date: str) -> bool:
    if conn is None or not run_date:
        return False
    row = conn.execute(
        "SELECT status FROM scheduled_runs WHERE job = ? AND run_date = ?", (job, run_date)
    ).fetchone()
    return bool(row) and str(row[0]) == STATUS_OK


def _record(
    conn: Any, job: str, run_date: str, status: str, detail: str, ts: str
) -> None:
    """写 ``scheduled_runs``：同 (job, run_date) 覆盖成**最后一次**结果。

    覆盖而不是追加，是因为这张表的用途只有两个——"跑过没有"与"上次为什么失败"；
    历史轨迹在 ``logs/jobs-*.jsonl`` 里，两边分工不重叠。
    """
    if conn is None:
        return
    with conn:
        conn.execute(
            "INSERT INTO scheduled_runs(job, run_date, status, detail, created_at)"
            " VALUES(?, ?, ?, ?, ?)"
            " ON CONFLICT(job, run_date) DO UPDATE SET"
            " status = excluded.status, detail = excluded.detail,"
            " created_at = excluded.created_at",
            (job, run_date, status, detail, ts),
        )


def _append_log(ctx: JobContext, entry: dict[str, Any]) -> Path:
    day = ctx.clock.now().astimezone().date()
    path = ctx.data_dir / JOB_LOG_DIRNAME / f"jobs-{day.isoformat()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


# ------------------------------------------------------------------ 执行器
async def run_job(name: str, fn: Callable[[], Awaitable[None]] | None = None) -> None:
    """跑一个 job：**先查幂等键，再执行，异常一律吞掉但必须留痕**（§10.3.2）。

    ``fn`` 默认取 ``JOBS`` 里同名任务的函数；显式传入是给用例与手工补跑用的。
    返回 ``None`` —— 调用方不该从返回值判断成败，"跑没跑、成没成"看
    ``scheduled_runs`` 与 ``logs/jobs-*.jsonl``，这两处是给人和运维看的。
    """
    ctx = context()
    spec = spec_for(name)
    task = fn or (spec.fn if spec else None)
    now = ctx.clock.now()
    key = idempotency_key(spec, now)
    job, run_date = _split_key(key) if key else (name, "")
    ts = to_local_iso(now)

    if _already_ok(ctx.conn, job, run_date):
        _append_log(
            ctx,
            {
                "ts": ts,
                "job": job,
                "key": key,
                "status": STATUS_SKIPPED,
                "detail": "今天已经成功跑过（scheduled_runs 命中）",
                "duration_ms": 0,
            },
        )
        return
    if task is None:  # 编程错误：名字写错了
        detail = f"没有这个 job：{name}"
        _record(ctx.conn, job, run_date, STATUS_FAILED, detail, ts)
        _append_log(
            ctx,
            {"ts": ts, "job": job, "key": key, "status": STATUS_FAILED, "detail": detail},
        )
        return

    started = time.perf_counter()
    status, detail = STATUS_OK, ""
    try:
        outcome = await task()
    except Exception as exc:  # 绝不外抛：巩固失败不能让用户发不出消息（§10.3.2）
        status = STATUS_FAILED
        detail = f"{type(exc).__name__}: {exc}"
    else:
        detail = outcome if isinstance(outcome, str) else ""
    duration_ms = int((time.perf_counter() - started) * 1000)
    _record(ctx.conn, job, run_date, status, detail, ts)
    _append_log(
        ctx,
        {
            "ts": ts,
            "job": job,
            "key": key,
            "status": status,
            "detail": detail,
            "duration_ms": duration_ms,
        },
    )


# ------------------------------------------------------------------ 三个任务
def ensure_runtime(ctx: JobContext | None = None) -> None:
    """跑任务前把记忆子系统装上（**幂等**：已经配过就不动）。

    调度进程多半是独立启动的（将来挂在 ``serve`` 或系统计划任务下），没人会替它
    调一次 ``memory.configure()``。缺了这一步，巩固与巡检会**天天 failed**——
    "失败可见"（§10.3.2）说的是不许静默，不是允许常态化失败。
    """
    from yixiang import memory

    if memory.is_configured():
        return
    ctx = ctx or context()
    memory.configure(ctx.conn, data_dir=ctx.data_dir, clock=ctx.clock, settings=ctx.settings)


async def job_consolidate() -> str:
    """23:30 巩固兜底：``force=True`` 表示"有多少蒸馏多少"，其余交给水印去重。

    **只有巩固本身失败（``failures`` / ``reason``）才算本 job 失败**；"今天没有新
    事实可写"是正常结果，记进 detail 就够了。把安静的每一天都标成 failed，等于
    让"失败可见"退化成噪音，第二天就没人看这一列了（§11.2 的同一条道理）。
    """
    ctx = context()
    from yixiang.memory.consolidate import consolidate

    ensure_runtime(ctx)
    result = await consolidate(
        ctx.conn,
        ctx.provider,
        settings=ctx.settings,
        clock=ctx.clock,
        force=True,
    )
    if result.failures or result.reason:
        raise RuntimeError(result.reason or f"巩固连续失败 {result.failures} 次")
    detail = result.summary()
    if result.warnings:
        detail = f"{detail} · " + "；".join(str(item) for item in result.warnings)
    return detail


async def job_daily_report() -> str:
    """23:50 每日汇总：把钱账写成 ``reports/usage-YYYY-MM-DD.json``。"""
    ctx = context()
    from yixiang.ops.usage import write_daily_report

    settings = ctx.settings
    budget = getattr(settings, "budget_cny_per_day", None)
    path = write_daily_report(
        ctx.data_dir / "usage.jsonl",
        ctx.data_dir / REPORTS_DIRNAME,
        ref=ctx.clock.now().astimezone().date(),
        budget_cny_per_day=budget,
        clock=ctx.clock,
    )
    return f"写入 {path.name}"


async def job_memory_verify() -> str:
    """周日 22:00 巡检：三文件 / 数据库 / 索引对不上就是 **failed**，不许静默。"""
    ctx = context()
    from yixiang.memory import sync

    ensure_runtime(ctx)
    problems = sync.verify(ctx.conn)
    if problems:
        raise RuntimeError(f"记忆三方对账不一致：{'；'.join(problems[:3])}")
    return "memory.md / 数据库 / FTS 索引三方对账一致"


JOBS: list[JobSpec] = [
    JobSpec(
        name="consolidate",
        cron="30 23 * * *",
        idempotency_key_tmpl="",  # 幂等由 §7.7.1 的水印保证，比日历更准
        fn=job_consolidate,
        summary="巩固兜底：当天没凑够阈值也要蒸馏一次",
    ),
    JobSpec(
        name="usage",
        cron="50 23 * * *",
        idempotency_key_tmpl="usage:{date}",
        fn=job_daily_report,
        summary="每日汇总：token / 成本 / 轮数写进 reports/",
    ),
    JobSpec(
        name="verify",
        cron="0 22 * * 0",
        idempotency_key_tmpl="verify:{week}",
        fn=job_memory_verify,
        summary="记忆巡检：memory.md / 数据库 / 索引三方对账（周日）",
    ),
]


# 晨报（§10.3.1）：SPEC 定义在 brief_job 里（内容层不 import 调度器），触发层在这里
# 把它接进表。放在模块**尾部**：brief_job 顶部要 `from yixiang.scheduler.jobs import
# JobSpec`，先有 JobSpec 再 import 才不会拿到半成品。
from yixiang.scheduler.brief_job import BRIEF_SPEC as _BRIEF_SPEC  # noqa: E402

JOBS.append(_BRIEF_SPEC)


__all__ = [
    "JOBS",
    "STATUS_FAILED",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "JobContext",
    "JobSpec",
    "configure",
    "context",
    "ensure_runtime",
    "idempotency_key",
    "job_consolidate",
    "job_daily_report",
    "job_memory_verify",
    "reset",
    "run_job",
    "spec_for",
]
