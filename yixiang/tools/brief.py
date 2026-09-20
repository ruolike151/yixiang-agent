"""``daily_brief``：按需组装的推荐日报（TECH §10.3 / PART-3 §4、§6 D19~D20）。

**这是内容层，不是触发层**（§10.3 最重要的架构约定）：本模块不 import 任何调度器、
不 import ``gateway``，只做三件事——读今日任务与到期备忘、挑 1 条影视推荐、落盘。
所以触发源从 cron 换成"用户问一句"时，这里一行都不用改；反过来，将来加回定时推送
也只是多一个调用方，不会产生第二份组装逻辑。

落盘口径（PART-3 §4 冻结）：``data/briefs/YYYY-MM-DD.md``，同一天重复生成**覆盖**
（一天只留一份，便于 diff 与回看）；推荐同时写 ``recommend_log``，这样"日报里推过的"
与"对话里推过的"共用同一个 7 天去重窗口（D-12）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from yixiang.rag import retrieve
from yixiang.tools import media, plan
from yixiang.tools.registry import error_text

SCOPES = ("today", "tomorrow")
BRIEF_DIRNAME = "briefs"


def daily_brief(
    conn: sqlite3.Connection,
    now: Callable[[], datetime],
    data_dir: Path | str,
    scope: str = "today",
) -> str:
    """组装 ``scope`` 那天的日报：今日安排 + 1 条影视推荐；写 briefs 文件与推荐日志。"""
    selected = str(scope or "today").strip().lower()
    if selected not in SCOPES:
        return error_text("bad_value", "scope", "只接受 today / tomorrow")
    moment = now()
    day = moment.date() + timedelta(days=1 if selected == "tomorrow" else 0)

    schedule = plan.list_today(conn, now, day.isoformat())
    hit = _pick_one(conn, moment)
    content = _render(day.isoformat(), schedule, hit, selected)

    path = Path(data_dir) / BRIEF_DIRNAME / f"{day.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")  # 同日覆盖：一天只留一份
    return content


def _pick_one(conn: sqlite3.Connection, moment: datetime) -> retrieve.MediaHit | None:
    """挑 1 条：走 ``retrieve_media`` 的 top-1（含口味加权与 7 天去重），并记日志。"""
    if not retrieve.is_configured() or media.corpus_size(conn) == 0:
        return None
    try:
        hits = retrieve.explain_search("", top_k=1, exclude_recent_days=7).ranked
    except Exception:  # 检索降级不该毁掉日报（§10.3.2 异常隔离的同一条纪律）
        return None
    if not hits:
        return None
    retrieve.log_recommendation(conn, [hits[0].id], channel="brief", moment=moment)
    return hits[0]


def _render(day: str, schedule: str, hit: retrieve.MediaHit | None, scope: str) -> str:
    """同一份内容既回给模型也落盘——两处不一致时排查成本更高。"""
    heading = f"# {day} 的日报" + ("（明天）" if scope == "tomorrow" else "")
    lines = [heading, "", "## 今日安排", schedule, "", "## 影视推荐"]
    if hit is None:
        lines.append("（影视库还是空的或近 7 天已推遍：先 `yixiang rag ingest` 入库，再问一次）")
    else:
        lines.append(media.wrap_external(f"1. {hit.render()}"))
    return "\n".join(lines) + "\n"


__all__ = ["BRIEF_DIRNAME", "SCOPES", "daily_brief"]
