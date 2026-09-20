"""调度：幂等、异常隔离、补发（PART-4 §3/§5/§7、TECH §10.3）。

这一组用例只断言调度器**自己的**契约，不重测巩固与巡检的质量（那是
``test_consolidation.py`` / ``test_memory_sync.py`` 的事）。四条主线：

  · **幂等键**：``usage:2026-09-19`` / ``verify:2026-W38`` 的字面量是冻结契约
    （PART-4 §4），改它等于改考核标准；巩固刻意**不用**日历键，它靠 §7.7.1 水印；
  · **同日重入只跑一次**：第二次触发写一行 ``skipped`` 日志，而不是"再跑一遍碰运气"；
  · **异常隔离**：job 抛异常只让这一行变 ``failed``，调用方拿到的仍是 ``None``
    （§10.3.2：巩固失败不能让用户发不出消息）；
  · **失败不占位**：``failed`` 之后的同键重试必须真跑起来（D-27 的补发前提）。

时间一律走 ``FixedClock``（2026-09-19 周六 10:00 +08:00），用例里没有 sleep。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fake_provider import FakeProvider, text_reply

from yixiang import memory
from yixiang.scheduler import brief_job, jobs

FIXED_DAY = "2026-09-19"
FIXED_WEEK = "2026-W38"  # 2026-09-19 所在的 ISO 周


def _run(coro):
    """把 ``run_job`` 这类协程同步跑完（调度器自己不管事件循环）。"""
    return asyncio.run(coro)


def _status(conn, job: str, run_date: str) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT status, detail FROM scheduled_runs WHERE job = ? AND run_date = ?",
        (job, run_date),
    ).fetchone()
    return (str(row["status"]), str(row["detail"] or "")) if row else None


def _log_lines(settings, day: str = FIXED_DAY) -> list[dict]:
    path = Path(settings.data_dir) / "logs" / f"jobs-{day}.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _configure(settings, conn, clock, provider):
    """装配调度上下文（用例按需换假 Provider：剧本得跟着题面走）。"""
    jobs.configure(
        conn,
        data_dir=settings.data_dir,
        clock=clock,
        settings=settings,
        provider=provider,
    )


@pytest.fixture
def scheduled(settings, conn, clock):
    """装配调度上下文（假时钟 + 内存库 + 假 Provider），用完清掉全局状态。"""
    _configure(settings, conn, clock, FakeProvider(text_reply("{}")))
    yield settings
    jobs.reset()
    memory.reset()


# ------------------------------------------------------------------ 幂等键
def test_idempotency_keys_match_the_frozen_format(clock):
    """PART-4 §4 的冻结键名：``usage:YYYY-MM-DD`` / ``verify:YYYY-Www``。"""
    now = clock.now()

    assert jobs.idempotency_key(jobs.spec_for("usage"), now) == f"usage:{FIXED_DAY}"
    assert jobs.idempotency_key(jobs.spec_for("verify"), now) == f"verify:{FIXED_WEEK}"
    # 巩固不用日历键：它的水印比"今天跑过没有"更准（§7.7.1）
    consolidate = jobs.spec_for("consolidate")
    assert jobs.idempotency_key(consolidate, now) == ""
    assert consolidate is not None and consolidate.deduped is False
    # 首月的调度表（TECH §10.3）：巩固兜底 23:30 / 汇总 23:50 / 巡检周日 22:00
    assert {spec.name: spec.cron for spec in jobs.JOBS} == {
        "consolidate": "30 23 * * *",
        "usage": "50 23 * * *",
        "verify": "0 22 * * 0",
    }


# ------------------------------------------------------------------ 幂等
def test_the_same_day_only_executes_once(scheduled, conn):
    runs: list[int] = []

    async def task() -> str:
        runs.append(1)
        return "第一次"

    assert _run(jobs.run_job("usage", task)) is None  # 契约：调用方不该靠返回值判成败
    _run(jobs.run_job("usage", task))  # 同日重入

    assert len(runs) == 1
    assert _status(conn, "usage", FIXED_DAY) == ("ok", "第一次")
    assert [entry["status"] for entry in _log_lines(scheduled)] == ["ok", "skipped"]


def test_a_skipped_run_says_why_in_the_job_log(scheduled):
    async def task() -> None:
        return None

    _run(jobs.run_job("usage", task))
    _run(jobs.run_job("usage", task))

    skipped = _log_lines(scheduled)[-1]
    assert skipped["job"] == "usage"
    assert skipped["key"] == f"usage:{FIXED_DAY}"
    assert "已经成功跑过" in skipped["detail"]


# ------------------------------------------------------------------ 异常隔离
def test_a_failing_job_never_reaches_the_caller(scheduled, conn):
    async def boom() -> None:
        raise RuntimeError("巩固炸了")

    _run(jobs.run_job("usage", boom))  # 不外抛：主链路继续活着

    status, detail = _status(conn, "usage", FIXED_DAY)
    assert status == "failed"
    assert detail == "RuntimeError: 巩固炸了"
    assert _log_lines(scheduled)[-1]["status"] == "failed"


def test_a_failed_job_does_not_take_the_slot(scheduled, conn):
    """失败不占位：下一次同键触发必须真跑（D-27 的补发就建在这条上）。"""
    attempts: list[str] = []

    async def flaky() -> str:
        attempts.append("try")
        if len(attempts) == 1:
            raise TimeoutError("模型超时")
        return "补上了"

    _run(jobs.run_job("usage", flaky))
    assert _status(conn, "usage", FIXED_DAY)[0] == "failed"

    _run(jobs.run_job("usage", flaky))
    assert len(attempts) == 2
    assert _status(conn, "usage", FIXED_DAY) == ("ok", "补上了")


def test_an_unknown_job_name_is_failed_not_silent(scheduled, conn):
    _run(jobs.run_job("nope"))

    status, detail = _status(conn, "nope", "")
    assert status == "failed"
    assert "没有这个 job" in detail


# ------------------------------------------------------------------ 真任务接线
def test_daily_report_job_writes_the_report_file(scheduled):
    """跑的是 ``JOBS`` 里真正的函数（不是注入的替身）：落盘路径也是契约的一部分。"""
    _run(jobs.run_job("usage"))

    path = Path(scheduled.data_dir) / "reports" / f"usage-{FIXED_DAY}.json"
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["ref"] == FIXED_DAY
    assert payload["period"] == "day"
    assert payload["budget_cny_per_day"] == scheduled.budget_cny_per_day


def _seed_chat(conn, count: int = 20) -> None:
    with conn:
        conn.executemany(
            "INSERT INTO chat_log(session_id, source, user_text, reply_text, tools_json,"
            " created_at) VALUES('cli:test', 'cli', ?, '嗯嗯', '[]',"
            " '2026-09-19T10:00:00+08:00')",
            [(f"第 {index} 轮闲聊",) for index in range(1, count + 1)],
        )


def test_consolidate_job_reports_a_quiet_day_as_ok(scheduled, conn, clock):
    """平时没新事实可写 = 正常结果，detail 里说清楚，**不是** failed。

    这一条挡的是"安静的每一天都被标红"：那样一周之后没人再看这一列（§11.2）。
    """
    _seed_chat(conn)
    batch = json.dumps(
        {"episode": {"summary": "闲聊了二十轮"}, "candidates": []}, ensure_ascii=False
    )
    _configure(scheduled, conn, clock, FakeProvider(text_reply(batch)))

    _run(jobs.run_job("consolidate"))

    status, detail = _status(conn, "consolidate", "")
    assert status == "ok", detail
    assert conn.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"] == 1
    assert "episode" in detail  # summary() 把"只写了情景记忆"说出来了


def test_consolidate_job_fails_when_the_model_returns_junk(scheduled, conn, clock):
    """坏 JSON 是**真失败**（水印不动），必须变成 failed 让下一天有人看见。"""
    _seed_chat(conn, 1)
    _configure(scheduled, conn, clock, FakeProvider(text_reply("模型今天不想输出结构化结果")))

    _run(jobs.run_job("consolidate"))

    status, detail = _status(conn, "consolidate", "")
    assert status == "failed"
    assert "巩固" in detail


def test_memory_verify_job_spots_drift(scheduled, conn):
    """巡检自己装配记忆子系统（调度进程独立启动时没人替它 configure）。"""
    data_dir = Path(scheduled.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "memory.md").write_text(
        "# 以湘的记忆\n\n## 用户\n- [99] 库里根本没有这条\n\n## 偏好\n", encoding="utf-8"
    )

    _run(jobs.run_job("verify"))

    status, detail = _status(conn, "verify", FIXED_WEEK)
    assert status == "failed"
    assert "对账不一致" in detail


# ------------------------------------------------------------------ P2 预留：补发
def test_brief_catchup_window_is_a_pure_function(clock):
    """D-27 的判定逻辑（§10.3.1）：纯函数，不看时钟也不写库，所以现在就能测。"""
    at = clock.now().replace

    assert brief_job.brief_should_run_now(at(hour=8, minute=0)) is True
    assert brief_job.brief_should_run_now(at(hour=11, minute=59)) is True
    assert brief_job.brief_should_run_now(at(hour=12, minute=1)) is False  # 过了补发窗口
    # 今天已经发过 → 一次都不补（幂等靠 brief:YYYY-MM-DD，见 brief_job）
    assert brief_job.brief_should_run_now(at(hour=9, minute=30), published_today=True) is False
    # 配置写坏了不许把补发一起崩掉
    assert brief_job.parse_catchup_until("不是时间") == brief_job.parse_catchup_until("12:00")
    assert brief_job.parse_catchup_until("9:30").hour == 9


def test_catchup_briefs_are_labelled():
    body = "今天有三件事。"

    assert brief_job.annotate_catchup(body, catchup=True).startswith(brief_job.CATCHUP_LABEL)
    assert brief_job.annotate_catchup(body, catchup=False) == body


def test_brief_job_stays_out_of_the_schedule():
    """P2 的东西不进本部分门禁（PART-4 §2）：改这一条之前先读附录 A-1。"""
    assert brief_job.BRIEF_JOB_NAME not in {spec.name for spec in jobs.JOBS}
    assert brief_job.BRIEF_SPEC.idempotency_key_tmpl == "brief:{date}"
    with pytest.raises(NotImplementedError):
        asyncio.run(brief_job.deliver_brief())


@pytest.mark.skip(reason="D-27 补发属 P2（PART-4 附录 A-1）：投递层未接，本阶段只测纯函数")
def test_d27_a_missed_brief_is_delivered_once_on_the_next_startup():
    """P2 启用时把这条从 skip 转实跑：8:00 未运行、9:30 启动 → 补发一次且带标注。

    届时接上投递层后的断言：

      · 第一次启动 → 发出 1 条，正文以 ``（补发）`` 开头；
      · 第二次启动 → 0 条（``scheduled_runs`` 命中 ``brief:YYYY-MM-DD`` + ``ok``）；
      · 12:00 之后启动 → 0 条（超出 ``YIXIANG_BRIEF_CATCHUP_UNTIL``）。
    """
