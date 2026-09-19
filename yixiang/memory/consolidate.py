"""巩固（Consolidation）：把 ``chat_log`` 蒸馏成 episode + 待写入的事实（TECH §7.7）。

三条不可动的规则：

  1. **水印只在整个批次成功后推进**（``meta.last_consolidated_chat_id``）——失败就
     原地重试，宁可慢一点，也不允许"巩固了一半"；
  2. **原始 ``chat_log`` 永不删除**：巩固是派生产物，不是迁移；
  3. **三档阈值**：``≥0.9`` 直接进正文、``0.6~0.9`` 进 ``## 待确认``、``<0.6`` 丢弃。
     宁可少写——记忆缺失下次再说一遍就行，污染会让助手"记错你"。

连续失败 3 次会写进 ``result.warnings``（日志/日报据此告警），绝不静默失败。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from yixiang import db
from yixiang.memory import semantic, sync
from yixiang.memory.sync import MEMORY_FILE
from yixiang.providers import ChatModel
from yixiang.runtime.models import (
    Clock,
    ProviderRequest,
    SystemClock,
    user_message,
)

WATERMARK_KEY = "last_consolidated_chat_id"
FAILURE_KEY = "consolidate_failures"

INPUT_CHARS = 500
MAX_CANDIDATES = 3
CONF_AUTO = 0.9
CONF_CONFIRM = 0.6
FAILURE_ALERT_AFTER = 3
UTILITY_MAX_TOKENS = 900

# 巩固 prompt：花括号走 ``.format()``，所以 JSON 示例里的花括号要转义
CONSOLIDATE_PROMPT = """你在做个人助手长期记忆的"巩固"：把下面一段对话蒸馏成
一条情景记忆（episode）+ 最多 {max_candidates} 条**跨会话仍然成立**的事实候选。

只输出这个 JSON，不要任何其他内容：
{{"episode": {{"summary": "一句话，20~60 字"}},
  "candidates": [{{"section": "用户|偏好", "content": "一句完整自洽的事实陈述", "confidence": 0.9}}]}}

质量约束（违反即算失败）：
1. 只提炼跨会话仍成立的事实（"今天学了 3 小时"是 episode，不是候选）；
2. 不总结情绪化临时表达（"今天好累"）；
3. 不产出与下面"现有记忆"重复的条目；
4. candidates 最多 {max_candidates} 条，宁少勿滥；
5. 不猜测：对话里没明说的偏好不要推断。宁可 candidates 为空数组。

现有记忆（content）：
{existing}

本批对话（各截断到 {input_chars} 字）：
{dialogue}"""


@dataclass(slots=True)
class ConsolidateResult:
    """一次巩固的结果；``ran=False`` 时 ``reason`` 说明为什么没跑。"""

    ran: bool = False
    turns: int = 0
    episode: bool = False
    auto_written: int = 0
    confirmed: int = 0
    dropped: int = 0
    duplicates: int = 0
    failures: int = 0
    last_chat_id: int = 0
    reason: str = ""
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.ran:
            return f"未巩固（{self.reason}）"
        return (
            f"巩固 {self.turns} 轮 → episode {'1' if self.episode else '0'} 条 · "
            f"正文 +{self.auto_written} · 待确认 +{self.confirmed} · "
            f"丢弃 {self.dropped} · 重复跳过 {self.duplicates}"
        )


# ------------------------------------------------------------------ 水印/失败计数
def watermark(conn: sqlite3.Connection) -> int:
    raw = db.get_meta(conn, WATERMARK_KEY, "0") or "0"
    try:
        return int(raw)
    except ValueError:
        return 0


def set_watermark(conn: sqlite3.Connection, chat_id: int) -> None:
    db.set_meta(conn, WATERMARK_KEY, str(chat_id))
    db.set_meta(conn, FAILURE_KEY, "0")


def failure_streak(conn: sqlite3.Connection) -> int:
    raw = db.get_meta(conn, FAILURE_KEY, "0") or "0"
    try:
        return int(raw)
    except ValueError:
        return 0


def record_failure(conn: sqlite3.Connection) -> int:
    streak = failure_streak(conn) + 1
    db.set_meta(conn, FAILURE_KEY, str(streak))
    return streak


# ------------------------------------------------------------------ 触发条件
def pending_rows(conn: sqlite3.Connection, *, limit: int = 500) -> list[sqlite3.Row]:
    """水印之后的未巩固对话（原始 ``chat_log`` 永不删除，这里只读）。"""
    return conn.execute(
        """
        SELECT id, session_id, user_text, reply_text, created_at
          FROM chat_log
         WHERE id > ? AND consolidated = 0
         ORDER BY id
         LIMIT ?
        """,
        (watermark(conn), limit),
    ).fetchall()


def should_consolidate(conn: sqlite3.Connection, settings: Any = None) -> bool:
    """未巩固轮数 ≥ ``settings.consolidate_every``（默认 20）才触发（§7.7.1）。"""
    every = int(getattr(settings, "consolidate_every", 20) or 20)
    return len(pending_rows(conn, limit=every)) >= max(1, every)


# ------------------------------------------------------------------ 主流程
async def consolidate(
    conn: sqlite3.Connection,
    provider: ChatModel | None,
    *,
    settings: Any = None,
    clock: Clock | None = None,
    force: bool = False,
) -> ConsolidateResult:
    """跑一次巩固。

    ``force=True`` 用于每日 23:30 的兜底与演示（只要有待巩固数据就跑）；
    默认按 ``consolidate_every`` 节流。
    """
    every = int(getattr(settings, "consolidate_every", 20) or 20)
    rows = pending_rows(conn, limit=500 if force else max(every, 1))
    if not rows:
        return ConsolidateResult(reason="没有未巩固的对话", failures=failure_streak(conn))
    if not force and len(rows) < max(1, every):
        return ConsolidateResult(
            reason=f"未巩固 {len(rows)} 轮 < 阈值 {every}",
            failures=failure_streak(conn),
        )
    if provider is None:
        return ConsolidateResult(reason="没有可用的模型", failures=failure_streak(conn))

    result = ConsolidateResult(ran=True, turns=len(rows))
    prompt = build_prompt(conn, rows)
    request = ProviderRequest(
        role="utility",
        messages=[user_message(prompt)],
        max_tokens=UTILITY_MAX_TOKENS,
        temperature=0.0,
        timeout=float(getattr(settings, "llm_timeout", 60.0) or 60.0),
        stream=False,
    )
    try:
        reply = await provider.complete(request)
        data = parse_batch(getattr(reply, "text", "") or "")
    except Exception as exc:  # 模型失败：水印不动，下次重试（§7.7.1）
        return _failed(conn, result, f"巩固调用失败：{exc}")
    if data is None:
        return _failed(conn, result, "巩固输出不是合法 JSON（水印不动，下次重试）")

    now = (clock or SystemClock()).now()
    ids = [int(row["id"]) for row in rows]
    try:
        _write_episode(data, rows, now)
        result.episode = True
        _write_candidates(conn, data, result)
        with conn:
            conn.executemany(
                "UPDATE chat_log SET consolidated = 1 WHERE id = ?",
                [(chat_id,) for chat_id in ids],
            )
        set_watermark(conn, ids[-1])  # 整个批次成功了才推水印（§7.7.1）
    except Exception as exc:
        return _failed(conn, result, f"巩固写库失败：{exc}")
    return result


def _failed(
    conn: sqlite3.Connection, result: ConsolidateResult, reason: str
) -> ConsolidateResult:
    streak = record_failure(conn)
    result.failures = streak
    result.reason = reason
    result.warnings.append(reason)
    if streak >= FAILURE_ALERT_AFTER:
        result.warnings.append(
            f"⚠️ 巩固已连续失败 {streak} 次（meta.{FAILURE_KEY}）——" "原始 chat_log 未丢，修好模型后重跑即可"
        )
    return result


# ------------------------------------------------------------------ prompt 构造
def build_prompt(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> str:
    existing = [
        str(row["content"]).strip()
        for row in conn.execute(
            "SELECT content FROM facts WHERE deleted = 0 ORDER BY id DESC LIMIT 50"
        ).fetchall()
    ]
    dialogue: list[str] = []
    for row in rows:
        user_text = str(row["user_text"] or "")[:INPUT_CHARS]
        reply_text = str(row["reply_text"] or "")[:INPUT_CHARS]
        dialogue.append(f"用户：{user_text}\n助手：{reply_text}")
    return CONSOLIDATE_PROMPT.format(
        max_candidates=MAX_CANDIDATES,
        input_chars=INPUT_CHARS,
        existing="\n".join(f"- {item}" for item in existing) or "（空）",
        dialogue="\n\n".join(dialogue),
    )


# ------------------------------------------------------------------ 输出校验
def parse_batch(text: str) -> dict[str, Any] | None:
    """严格解析：缺字段即判定失败（不猜、不补）。"""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    episode = data.get("episode")
    candidates = data.get("candidates", [])
    if not isinstance(episode, dict) or not str(episode.get("summary") or "").strip():
        return None
    if not isinstance(candidates, list):
        return None
    clean: list[dict[str, Any]] = []
    for item in candidates[:MAX_CANDIDATES]:
        if not isinstance(item, dict):
            return None
        section = str(item.get("section") or "").strip()
        content = str(item.get("content") or "").strip()
        confidence = item.get("confidence")
        if not section or not content or not isinstance(confidence, (int, float)):
            return None
        clean.append({"section": section, "content": content, "confidence": float(confidence)})
    return {"episode": {"summary": str(episode["summary"]).strip()}, "candidates": clean}


# ------------------------------------------------------------------ 落库
def _write_episode(data: dict[str, Any], rows: list[sqlite3.Row], now: datetime) -> None:
    from yixiang.memory import episodic

    last = rows[-1]
    happened = str(last["created_at"] or "") or None
    episodic.add_episode(
        happened or now.isoformat(timespec="seconds"),
        str(data["episode"]["summary"]),
        session_id=str(last["session_id"] or ""),
        source_chat_id=int(last["id"]),
    )


def _write_candidates(
    conn: sqlite3.Connection, data: dict[str, Any], result: ConsolidateResult
) -> None:
    store = semantic.FactStore(semantic.context())
    known = {
        str(row["content"]).strip().lower()
        for row in conn.execute("SELECT content FROM facts WHERE deleted = 0").fetchall()
    }
    written = 0
    for item in data["candidates"]:
        confidence = float(item["confidence"])
        content = str(item["content"]).strip()
        if confidence < CONF_CONFIRM:
            result.dropped += 1
            continue
        if content.lower() in known:
            result.duplicates += 1
            continue
        if confidence >= CONF_AUTO:
            subject = "偏好" if item["section"] == "偏好" else "用户"
        else:
            subject = "待确认"  # 0.6~0.9：给人一个低成本审阅动作
        store.insert(subject, content, source="consolidate", touch=False)
        known.add(content.lower())
        written += 1
        if subject == "待确认":
            result.confirmed += 1
        else:
            result.auto_written += 1
    if written:
        sync.write_memory_doc(conn)  # 双写第二跳：DB 权威 → memory.md
    else:
        result.warnings.append(f"{MEMORY_FILE} 无新增条目（只有 episode 写入）")


__all__ = [
    "CONF_AUTO",
    "CONF_CONFIRM",
    "ConsolidateResult",
    "FAILURE_KEY",
    "INPUT_CHARS",
    "MAX_CANDIDATES",
    "WATERMARK_KEY",
    "build_prompt",
    "consolidate",
    "failure_streak",
    "parse_batch",
    "pending_rows",
    "record_failure",
    "set_watermark",
    "should_consolidate",
    "watermark",
]
