"""SessionManager：工作记忆装配、历史窗口、会话生命周期（TECH-DESIGN §6）。

三条硬约束：
  1. **每轮现拼，不缓存**——system 段与历史窗口都在 ``assemble()`` 时重新计算，
     这样用户手改 ``data/*.md`` 或切换会话立刻生效（§6.1）。
  2. **静态在前、动态在后**——注入顺序固定为 S1→S8，时间与检索结果放最后，
     否则自动前缀缓存永远命中不了（§4.5）。
  3. **工具结果不进历史**——历史里只留 §5.3 的折叠摘要，完整 tool result 只在
     当轮上下文里存在（省 token、防缓存击穿）。
"""

from __future__ import annotations

import json
import sqlite3

from yixiang.config import Settings
from yixiang.runtime.models import (
    Clock,
    Message,
    SystemClock,
    assistant_message,
    to_local_iso,
    user_message,
)

# 单轮上下文软上限（字符）：超出时从最旧的一轮开始丢（§15.1 的粗略口径）。
CONTEXT_BUDGET_CHARS = 24_000

SOUL_LEARNED_MARKER = "## Learned rules"
CORE_FILES = ("soul.md", "user.md", "memory.md")

WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def read_core_file(settings: Settings, name: str) -> str:
    """读 ``data/<name>``；缺文件时回落到 ``templates/<name>``。

    实体复制由 ``yixiang doctor`` 完成（§1.2 检查项 6），这里只保证"读不到不炸"。
    """
    for path in (settings.data_dir / name, settings.templates_dir / name):
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return ""


def split_soul(soul: str) -> tuple[str, str]:
    """把 soul.md 拆成 S1（身份 + 行为守则）与 S2（Learned rules，只追加）。"""
    index = soul.find(SOUL_LEARNED_MARKER)
    if index < 0:
        return soul, ""
    return soul[:index].strip(), soul[index:].strip()


class SessionManager:
    """一个会话的工作记忆装配器 + 历史窗口。"""

    def __init__(
        self,
        settings: Settings,
        *,
        store: sqlite3.Connection | None = None,
        session_id: str = "cli:default",
        source: str = "cli",
        clock: Clock | None = None,
        budget_chars: int = CONTEXT_BUDGET_CHARS,
    ) -> None:
        self.settings = settings
        self.store = store
        self.clock = clock or SystemClock()
        self.session_id = session_id
        self.source = source
        # 单轮上下文软上限（可注入：D-21 用收紧的预算验证裁剪顺序）
        self.budget_chars = budget_chars
        self.turn_id = ""
        self.pending_user = ""
        self.history: list[Message] = []
        self._last_working_memory: dict[str, object] = {}
        self.load_history()

    # ------------------------------------------------------------------ 会话
    def switch(self, session_id: str, *, source: str | None = None) -> None:
        self.session_id = session_id
        if source:
            self.source = source
        self.pending_user = ""
        self.history = []
        self.load_history()

    def new_session_id(self, name: str | None = None) -> str:
        stamp = self.clock.now().strftime("%Y%m%d-%H%M")
        suffix = (name or "").strip().replace(" ", "-")
        return f"{self.source}:{stamp}" + (f"-{suffix}" if suffix else "")

    def new_session(self, name: str | None = None) -> str:
        self.switch(self.new_session_id(name))
        return self.session_id

    def list_sessions(self, limit: int = 20) -> list[dict[str, object]]:
        """历史会话列表：标题 = 该会话首条用户消息前 60 字（§10.1 ``/history``）。"""
        if self.store is None:
            return []
        rows = self.store.execute(
            """
            SELECT session_id,
                   COUNT(*)                       AS turns,
                   MIN(id)                        AS first_id,
                   MAX(created_at)                AS last_at
              FROM chat_log
             GROUP BY session_id
             ORDER BY last_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        sessions: list[dict[str, object]] = []
        for row in rows:
            first = self.store.execute(
                "SELECT user_text FROM chat_log WHERE id = ?", (row["first_id"],)
            ).fetchone()
            title = (first["user_text"] if first else "") or ""
            sessions.append(
                {
                    "session_id": row["session_id"],
                    "turns": row["turns"],
                    "last_at": row["last_at"],
                    "title": title[:60],
                }
            )
        return sessions

    # ------------------------------------------------------------------ 历史
    def load_history(self, turns: int | None = None) -> None:
        """从 ``chat_log`` 读回最近 N 轮（``/history`` 与会话切换的重建路径）。"""
        self.history = []
        if self.store is None:
            return
        limit = self.settings.history_turns if turns is None else turns
        if limit <= 0:
            return
        rows = self.store.execute(
            """
            SELECT user_text, reply_text FROM chat_log
             WHERE session_id = ?
             ORDER BY id DESC LIMIT ?
            """,
            (self.session_id, limit),
        ).fetchall()
        for row in reversed(rows):
            self.history.append(user_message(row["user_text"]))
            self.history.append(assistant_message(row["reply_text"]))

    def begin_turn(self, user_text: str, *, turn_id: str = "") -> str:
        self.pending_user = user_text
        if turn_id:
            self.turn_id = turn_id
        return self.turn_id

    def add_exchange(self, user: str, reply: str, tools: list[object] | None = None) -> None:
        """把一轮对话写回历史与 ``chat_log``（工具痕迹已折叠进 ``reply``）。"""
        self.history.append(user_message(user))
        self.history.append(assistant_message(reply))
        if self.store is None:
            return
        tools_json = json.dumps(
            [t.as_trace() for t in (tools or [])], ensure_ascii=False
        )
        with self.store:
            self.store.execute(
                """
                INSERT INTO chat_log(session_id, source, user_text, reply_text,
                                     tools_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.session_id,
                    self.source,
                    user,
                    reply,
                    tools_json,
                    to_local_iso(self.clock.now()),
                ),
            )

    # -------------------------------------------------------------- 工作记忆
    def system_blocks(self) -> list[str]:
        """§6.1 的 S1~S8：**永远返回 8 段**（空段保留占位，便于 trace 对齐）。"""
        soul_raw = read_core_file(self.settings, "soul.md")
        s1, s2 = split_soul(soul_raw)
        blocks = [
            s1,
            s2,
            read_core_file(self.settings, "user.md"),
            read_core_file(self.settings, "memory.md"),
            self.skills_block(),
            self.retrieved_block(),
            self.environment_block(),
            self.turn_contract_block(),
        ]
        return blocks

    def skills_block(self) -> str:
        """S5 相关技能（关键词匹配 ``data/skills/*/SKILL.md``）——PART 2 接入。"""
        return ""

    def retrieved_block(self) -> str:
        """S6 检索到的记忆（gate 命中后的 facts + episodes）——PART 2 接入。"""
        return ""

    def environment_block(self) -> str:
        """S7 环境信息：精确到分钟，放 system 末尾（§4.5）。"""
        now = self.clock.now()
        offset = now.strftime("%z") or "+0000"
        zone = f"UTC{offset[:3]}:{offset[3:]}" if len(offset) == 5 else "UTC"
        return f"当前时间：{now:%Y-%m-%d %H:%M}（{WEEKDAY_ZH[now.weekday()]}，{zone}）"

    def turn_contract_block(self) -> str:
        """S8 本轮契约（"记住"指令的硬性要求等）——PART 2 接入。"""
        return ""

    def window(self) -> list[Message]:
        """历史窗口：最近 ``history_turns`` 轮（user+assistant 成对）。"""
        limit = max(self.settings.history_turns, 0) * 2
        if limit == 0:
            return []
        return list(self.history[-limit:])

    def assemble(self) -> list[Message]:
        """每轮现拼：历史窗口 + 本轮用户消息（§6.2）。

        预算口径必须与 ``ProviderRequest.input_chars()`` 一致（system 用 ``\\n\\n``
        拼好后的实际长度），并且先扣掉本轮用户消息——否则"input ≤ 预算"这条
        契约会被尾部那条 user 消息悄悄击穿（D-21 断言的就是它）。
        裁剪以**整轮**为单位：丢掉最旧的一对 user/assistant，绝不切半轮。
        """
        blocks = self.system_blocks()
        system_chars = len("\n\n".join(block for block in blocks if block))
        budget = self.budget_chars - system_chars - len(self.pending_user)
        history = self.window()
        while len(history) > 2 and sum(len(m.content or "") for m in history) > budget:
            history = history[2:]  # 丢最旧的一轮
        self._last_working_memory = self._snapshot(blocks, history)
        return history + [user_message(self.pending_user)]

    def working_memory(self) -> dict[str, object]:
        """§6.3 的 trace 快照（只有分段长度，没有全文）。"""
        return dict(self._last_working_memory)

    def _snapshot(self, blocks: list[str], history: list[Message]) -> dict[str, object]:
        return {
            "s1": len(blocks[0]),
            "s2": len(blocks[1]),
            "s3": len(blocks[2]),
            "s4": len(blocks[3]),
            "s5": len(blocks[4]),
            "s6": {"facts": 0, "episodes": 0, "chars": len(blocks[5])},
            "s7": len(blocks[6]),
            "s8": len(blocks[7]),
            "history_turns": len(history) // 2,
        }


def load_core_files(settings: Settings) -> dict[str, str]:
    """一次性读三文件（doctor / 调试用）。"""
    return {name: read_core_file(settings, name) for name in CORE_FILES}
