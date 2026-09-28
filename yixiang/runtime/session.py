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
import traceback
from typing import Any

from yixiang import memory
from yixiang.config import Settings
from yixiang.memory import core_files, procedural
from yixiang.memory.core_files import SOUL_LEARNED_MARKER
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

# 单条用户消息的硬上限（字符）：§14.2 T-8。超长粘贴（整篇 PDF / 一屏日志）如果
# 原样进 prompt，会一次性吃掉整个上下文预算，把记忆段和历史全挤掉；所以入口处
# 就截断，并把"丢了多少字"记进 trace（可见，不是静默）。
USER_INPUT_LIMIT = 6000
USER_TRUNCATED_NOTICE = "…（消息过长，已截断）"

# 会话标题上限（它会进历史列表、进导出文件的第一行，不该无限长）
TITLE_LIMIT = 60

# 导出一次最多带多少轮（比历史窗口大得多，但仍然是有限的：别让一个 10 万轮的会话
# 把内存和浏览器一起拖死）
EXPORT_TURN_LIMIT = 10_000

CORE_FILES = ("soul.md", "user.md", "memory.md")

WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# S6 检索上限（§6.1）
RETRIEVAL_TOP_K = 5
RETRIEVAL_EP_K = 3

# ── §7.9.1 阶段 1：纯规则打标（不花一次 LLM 调用）──
REMEMBER_PREFIXES = ("记住", "帮我记住", "别忘了", "记一下我")
MEMO_WORDS = ("截止", "之前", "提醒", "周五", "明天")
TURN_CONTRACT_REMEMBER = (
    "【本轮硬性要求】用户明确要求记住一条信息。\n"
    "你必须先调用 manage_memory(action=\"search\", query=\"<要记住的内容核心词>\") 检查是否已有相近记忆：\n"
    "  - 已有相近记忆 → 调 manage_memory(action=\"update\", id=<该条 id>, content=\"<更新后的完整表述>\")\n"
    "  - 没有 → 调 save_memory(subject=\"<用户|偏好|项目|其他>\", content=\"<简洁的事实陈述>\")\n"
    "在工具成功返回前，不得结束本轮。回复中必须复述\"已记住：<内容>\"。\n"
    "如果写入失败，必须如实说明失败，禁止声称已记住。"
)

# 用户点名要动记忆文件本身（而不是"记住某件事"）——这时唯一的落盘出口是 edit
MEMORY_FILE_WORDS = ("memory.md", "memory.MD", "记忆文件", "记忆.md", "备忘文件", "记忆文档")
MEMORY_EDIT_WORDS = (
    "改",
    "编辑",
    "删",
    "去掉",
    "移除",
    "加",
    "补充",
    "整理",
    "清理",
    "重排",
    "合并",
    "更新",
    "写回",
)
TURN_CONTRACT_MEMORY_EDIT = (
    "【本轮硬性要求】用户明确要求改记忆文件本身（data/memory.md）。\n"
    "只能调 manage_memory(action=\"edit\", ...) 真正落盘，op 只有三个：\n"
    "  - 加一行：manage_memory(action=\"edit\", op=\"add\", section=\"<用户|偏好|待确认|手写笔记>\", content=\"<正文>\")\n"
    "  - 换一行：manage_memory(action=\"edit\", op=\"replace\", match=\"<原文片段或 [id]>\", content=\"<新正文>\")\n"
    "  - 删一行：manage_memory(action=\"edit\", op=\"remove\", match=\"<原文片段或 [id]>\")\n"
    "只在回复里列一份\"更新后\"的清单等于没改：文件没动，用户下次看到的还是原来那份。\n"
    "必须真调工具，并按工具返回如实汇报（多行命中/没命中时它会给候选，照着改小 match 再试）。\n"
    "只有用户明确要求时才用 edit；不要自己兴起整理记忆。"
)

# 用户要求"改期"（把某条任务 / 备忘挪到另一天）。这一类话的失败模式特别隐蔽：
# 模型回一句"挪好了"，库里那一列没动，用户下次打开还是老日期——所以打标 + 契约，
# 让"说了挪就必须真挪"变成可断言的行为（与 memory edit 那份契约同一个范式）。
RESCHEDULE_WORDS = ("挪到", "挪去", "挪一下", "改到", "改期", "推迟", "顺延")
TURN_CONTRACT_RESCHEDULE = (
    "【本轮硬性要求】用户要求把某条任务或备忘**改期**（挪到 / 改到 / 推迟 / 顺延）。\n"
    "改期只有这两个出口，必须真调，并在工具成功返回后如实汇报改到了哪天：\n"
    "  - 任务：先 list_today 拿 item_id → reschedule_task(item_id=<id>, date=\"YYYY-MM-DD\")\n"
    "  - 备忘：先 list_memos 拿 id → reschedule_memo(id=<id>, due_at=\"YYYY-MM-DD 或 周五中午\")\n"
    "date / due_at 要落到具体某一天：'本周'这类说法按当前时间换算成日期再传。\n"
    "只在回复里说\"已经挪好了\"等于没挪——库里没动，用户下次看到的还是老日期。\n"
    "库里没有对应条目就如实说没有，不要编。"
)

# §7.9 阶段 3：后验校验没过时追加的系统提醒（纠错重试 1 次）
MEMORY_RETRY_NOTICE = (
    "【提醒】上一轮你没有把用户要求记住的信息写进长期记忆。"
    "现在必须先调用 manage_memory(action=\"search\") 或 save_memory 完成写入，再回复用户。"
)

# 改期轮的后验校验没过时追加的系统提醒（与"记住"轮同一个纠错范式）
RESCHEDULE_RETRY_NOTICE = (
    "【提醒】上一轮你说把条目改期了，但并没有真正调用工具——库里那一列还是老日期。"
    "现在必须真的调用 reschedule_task（任务）或 reschedule_memo（备忘）完成改期；"
    "若解析不出具体某一天，就如实问用户是哪一天，不要编。"
)


def detect_intent(message: str) -> str:
    """打标：显式的"记住"指令 → ``"REMEMBER"``，点名改记忆文件 → ``"MEMORY_EDIT"``。

    含时间意图的待办（"记一下周五交材料"）归 memo 路径，不打标——这是
    "记住我喜欢悬疑"与"记一下周五交材料"的分界线，靠规则而不是模型判断。
    "改记忆文件"与"记住某件事"同理：前者要动 ``memory.md`` 正文（edit），
    后者是往库里加一条事实（save_memory），两条路不能混。

    改期（``"RESCHEDULE"``）排在最前面：'挪到 / 改到 / 推迟'比"记住"这个前缀
    具体得多，先认它们——"记住把周五那条挪到本周"也不会被误判成 REMEMBER。
    """
    text = (message or "").strip()
    if any(word in text for word in RESCHEDULE_WORDS):
        return "RESCHEDULE"
    if any(text.startswith(prefix) for prefix in REMEMBER_PREFIXES):
        if any(word in text for word in MEMO_WORDS):
            return ""
        return "REMEMBER"
    # 只是提到 memory.md（"我这边 memory.md 还是原来的"）不算要求，必须带动作词
    if any(word in text for word in MEMORY_FILE_WORDS) and any(
        word in text for word in MEMORY_EDIT_WORDS
    ):
        return "MEMORY_EDIT"
    return ""


def _with_image_note(text: str, images: list[str]) -> str:
    """把"（附图：…）"接到用户消息末尾。

    图片本身随消息发一次（多模态），历史里只留这一行：刷新页面、切会话、下一轮
    再问"刚才那张图"时，人和模型都还看得见那一轮带了哪张图、在哪儿；而 base64
    不会被每轮重发一遍（前缀缓存与上下文预算都不答应）。
    """
    note = "（附图：" + "、".join(images) + "）"
    return f"{text.rstrip()}\n{note}" if (text or "").strip() else note


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
        self.pending_images: list[str] | None = None
        self.turn_intent = ""
        self.extra_contract = ""
        self.truncated_chars = 0
        self.history: list[Message] = []
        self._last_working_memory: dict[str, object] = {}
        self._skill_loader: Any = None
        self._retrieval: Any = None
        self._retrieval_query = ""
        self._retrieval_allowed = False
        self._retrieval_done = False
        self._retrieval_error = ""
        self.load_history()

    # ------------------------------------------------------------------ 会话
    def switch(self, session_id: str, *, source: str | None = None) -> None:
        self.session_id = session_id
        if source:
            self.source = source
        self.pending_user = ""
        self.pending_images = None
        self.history = []
        self.load_history()

    def new_session_id(self, name: str | None = None, *, source: str | None = None) -> str:
        """新会话的 id：``<来源>:<时间戳>[-名字]``。

        ``source`` 不传就沿用当前来源（CLI 的 ``/new`` 仍落在 ``cli:``）；Web 控制台
        会明确传 ``web``——否则"站在 QQ 会话里点新会话"会造出 ``qq:<时间戳>``，
        既把 QQ 的往来拆成好几条，又让来源标记说假话。
        """
        stamp = self.clock.now().strftime("%Y%m%d-%H%M")
        suffix = (name or "").strip().replace(" ", "-")
        origin = source or self.source
        return f"{origin}:{stamp}" + (f"-{suffix}" if suffix else "")

    def new_session(self, name: str | None = None, *, source: str | None = None) -> str:
        self.switch(self.new_session_id(name, source=source), source=source)
        return self.session_id

    def list_sessions(self, limit: int = 20) -> list[dict[str, object]]:
        """历史会话列表：标题优先取用户改过的，回落该会话首条用户消息前 60 字（§10.1）。"""
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
        titles = self._title_map()
        return [self._session_row(row, titles) for row in rows]

    def search_sessions(self, query: str, *, limit: int = 30) -> list[dict[str, object]]:
        """按标题或正文找会话（子串匹配，大小写不敏感）；空查询 = 列出全部。

        正文两列都查：用户常记得的是"它当时怎么回的"，只搜标题会漏掉一大半。
        命中的是"哪个会话"，所以行上的 ``turns`` / 默认标题仍然按该会话**全部**往来算
        （拿筛过的那几行去 ``COUNT(*)``，搜"召回"会把 2 轮的会话显示成 1 轮）。
        """
        text = (query or "").strip()
        if self.store is None:
            return []
        if not text:
            return self.list_sessions(limit=limit)
        like = f"%{text}%"
        rows = self.store.execute(
            """
            SELECT session_id,
                   COUNT(*)        AS turns,
                   MIN(id)         AS first_id,
                   MAX(created_at) AS last_at
              FROM chat_log
             WHERE session_id IN (
                       SELECT session_id FROM chat_log
                        WHERE user_text LIKE ? COLLATE NOCASE
                           OR reply_text LIKE ? COLLATE NOCASE
                       UNION
                       SELECT session_id FROM session_titles
                        WHERE title LIKE ? COLLATE NOCASE)
             GROUP BY session_id
             ORDER BY last_at DESC
             LIMIT ?
            """,
            (like, like, like, limit),
        ).fetchall()
        titles = self._title_map()
        return [self._session_row(row, titles) for row in rows]

    def _title_map(self) -> dict[str, str]:
        """用户改过的标题：一次取回，避免每行一条 SELECT。"""
        if self.store is None:
            return {}
        return {
            str(row["session_id"]): str(row["title"])
            for row in self.store.execute(
                "SELECT session_id, title FROM session_titles"
            ).fetchall()
        }

    def _session_row(self, row: sqlite3.Row, titles: dict[str, str]) -> dict[str, object]:
        """一行会话：``list_sessions`` 与 ``search_sessions`` 共用同一套拼装。

        顺带带上 ``source``（首轮是从哪个入口进来的）：Web 历史面板要能一眼分清
        "这条是 QQ 上聊的、那条是网页上聊的"，光看 ``qq:1904625008`` 这种 id 认不出来。
        """
        first = self.store.execute(
            "SELECT user_text, source FROM chat_log WHERE id = ?", (row["first_id"],)
        ).fetchone()
        default_title = str((first["user_text"] if first else "") or "")[:TITLE_LIMIT]
        return {
            "session_id": row["session_id"],
            "turns": row["turns"],
            "last_at": row["last_at"],
            "source": str(first["source"]) if first else "",
            "title": titles.get(str(row["session_id"])) or default_title,
        }

    def rename_session(self, session_id: str, title: str) -> bool:
        """给会话起个人看得懂的名字；标题传空串 = 删掉自定义标题，回落默认。

        返回 ``False`` 表示"这条会话现在没有自定义标题了"——不是失败：用户把输入框
        清空再确定，要的就是回到"首条用户消息前 60 字"。
        """
        if self.store is None:
            return False
        text = str(title or "").strip()[:TITLE_LIMIT]
        with self.store:
            if not text:
                self.store.execute(
                    "DELETE FROM session_titles WHERE session_id = ?", (session_id,)
                )
                return False
            self.store.execute(
                """
                INSERT INTO session_titles(session_id, title, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    title = excluded.title, updated_at = excluded.updated_at
                """,
                (session_id, text, to_local_iso(self.clock.now())),
            )
        return True

    def delete_session(self, session_id: str) -> int:
        """删掉一个会话的往来记录，返回删掉多少轮。

        **只删 ``chat_log`` 与该会话的标题**：``facts``（长期记忆）与 ``episodes``
        （片段）是这个人的记忆本身，不属于某一次会话——删历史不该顺手抹掉
        "他喜欢悬疑"。要清记忆请走 ``manage_memory`` / ``yixiang memory``。
        """
        if self.store is None:
            return 0
        with self.store:
            cursor = self.store.execute(
                "DELETE FROM chat_log WHERE session_id = ?", (session_id,)
            )
            self.store.execute(
                "DELETE FROM session_titles WHERE session_id = ?", (session_id,)
            )
        return int(cursor.rowcount or 0)

    def export_session(self, session_id: str) -> dict[str, object]:
        """某个会话的全部往来（导出用：正序、全量，不套历史窗口）。"""
        return {
            "session_id": session_id,
            "exported_at": to_local_iso(self.clock.now()),
            "turns": self._turns(session_id, EXPORT_TURN_LIMIT),
        }

    def source_of(self, session_id: str, default: str = "cli") -> str:
        """某个会话原先的来源标记（Web 控制台切回旧会话时别改写它的 source）。

        ``chat_log.source`` 记的是"这句话从哪个入口进来的"：切到一个 cli 会话
        继续聊，新消息仍应标成 ``cli``，否则 trace 里的来源会随入口漂移。
        """
        if self.store is None:
            return default
        row = self.store.execute(
            "SELECT source FROM chat_log WHERE session_id = ? ORDER BY id LIMIT 1",
            (session_id,),
        ).fetchone()
        return str(row["source"]) if row else default

    def transcript(self, *, limit: int = 200) -> list[dict[str, object]]:
        """当前会话的完整往来（Web 控制台"查看历史对话"）。

        与 ``/history`` 同一张表，但**不套历史窗口**：``load_history`` 只取最近
        ``history_turns`` 轮给模型用，这里要的是"这一整天聊了什么"。
        返回按时间正序（旧 → 新），元素是 ``{id, user, reply, tools, at}``。
        """
        return self._turns(self.session_id, limit)

    def _turns(self, session_id: str, limit: int) -> list[dict[str, object]]:
        """某个会话最近 ``limit`` 轮，返回按时间正序（旧 → 新）。

        ``transcript()`` 与 ``export_session()`` 共用这一处取数：同一个"读往来"的
        口径只该有一份实现，否则导出的顺序迟早和面板上看到的对不上。
        """
        if self.store is None:
            return []
        rows = self.store.execute(
            """
            SELECT id, user_text, reply_text, tools_json, created_at
              FROM chat_log
             WHERE session_id = ?
             ORDER BY id DESC LIMIT ?
            """,
            (session_id, max(int(limit), 1)),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "user": row["user_text"],
                "reply": row["reply_text"],
                "tools": _tools_of(row["tools_json"]),
                "at": row["created_at"],
            }
            for row in reversed(rows)
        ]

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

    def begin_turn(
        self, user_text: str, *, turn_id: str = "", images: list[str] | None = None
    ) -> str:
        """起一轮：**入口截断**超长输入，打标意图，清掉上一轮的检索状态。

        截断发生在这里而不是 ``add_exchange`` / ``assemble``：用户消息在
        prompt、``chat_log``、trace 三处必须是同一个字符串，否则"检索用了什么"
        与"记下来的是什么"会对不上（T-8 的断言就打在这一点上）。

        ``images`` 是本轮附图（``data/`` 下的相对路径）。图片随消息发出去，
        文本里只留一行"（附图：…）"——所以它会同时进 prompt、chat_log 与 trace。
        """
        text = user_text or ""
        self.truncated_chars = max(len(text) - USER_INPUT_LIMIT, 0)
        if self.truncated_chars:
            text = text[:USER_INPUT_LIMIT] + USER_TRUNCATED_NOTICE
        self.pending_images = [str(item) for item in images] if images else None
        if self.pending_images:
            # 附图这一行接在截断**之后**：它是"这一轮带了什么"的凭据，不能先被砍掉
            text = _with_image_note(text, self.pending_images)
        self.pending_user = text
        if turn_id:
            self.turn_id = turn_id
        self.turn_intent = detect_intent(text)
        self.extra_contract = ""
        self.prime_retrieval("", allowed=False)  # App 门控后会覆盖
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
            core_files.core_memory_text(read_core_file(self.settings, "memory.md")),
            self.skills_block(),
            self.retrieved_block(),
            self.environment_block(),
            self.turn_contract_block(),
        ]
        return blocks

    def skills_block(self) -> str:
        """S5 相关技能（关键词匹配 ``data/skills/*/SKILL.md``，≤1500 字）。"""
        loader = self.skill_loader()
        if loader is None:
            return ""
        try:
            matched = loader.match(self.pending_user, max_skills=procedural.MATCH_LIMIT)
        except Exception:  # 技能坏了不该炸掉整轮对话
            return ""
        return procedural.render_block(matched)

    def skill_loader(self):
        """按 ``settings.skills_dir`` 建一次 ``SkillLoader``（mtime 变了会自己重读）。"""
        if self._skill_loader is None:
            self._skill_loader = procedural.SkillLoader([self.settings.skills_dir])
        return self._skill_loader

    def retrieved_block(self) -> str:
        """S6 检索到的记忆（gate 命中后的 facts + episodes）。

        ``system_blocks()`` 每次迭代都被调，所以真正的检索**一轮只做一次**：
        结果缓存在 ``self._retrieval``，下一轮 ``begin_turn()`` 清掉。
        """
        if not self._retrieval_allowed or not memory.is_configured():
            return ""
        if not self._retrieval_done:
            self._retrieval_done = True
            try:
                self._retrieval = memory.retrieve_memory(
                    self._retrieval_query or self.pending_user,
                    top_k=RETRIEVAL_TOP_K,
                    ep_k=RETRIEVAL_EP_K,
                )
            except Exception:  # 嵌入不可用/未装配：降级成"没有检索结果"
                self._retrieval = None
                self._retrieval_error = traceback.format_exc(limit=1).strip().splitlines()[-1]
        return self._retrieval.render() if self._retrieval else ""

    def prime_retrieval(self, query: str = "", *, allowed: bool = True) -> None:
        """门控结果进来（App 在 ``run_loop`` 之前调）：本轮要不要检索、检索什么。"""
        self._retrieval_allowed = allowed
        self._retrieval_query = query
        self._retrieval_done = False
        self._retrieval = None
        self._retrieval_error = ""

    def environment_block(self) -> str:
        """S7 环境信息：精确到分钟，放 system 末尾（§4.5）。"""
        now = self.clock.now()
        offset = now.strftime("%z") or "+0000"
        zone = f"UTC{offset[:3]}:{offset[3:]}" if len(offset) == 5 else "UTC"
        return f"当前时间：{now:%Y-%m-%d %H:%M}（{WEEKDAY_ZH[now.weekday()]}，{zone}）"

    def turn_contract_block(self) -> str:
        """S8 本轮契约：``intent = REMEMBER`` 或纠错重试提醒（≤300 字）。"""
        parts: list[str] = []
        if self.turn_intent == "REMEMBER":
            parts.append(TURN_CONTRACT_REMEMBER)
        if self.turn_intent == "MEMORY_EDIT":
            parts.append(TURN_CONTRACT_MEMORY_EDIT)
        if self.turn_intent == "RESCHEDULE":
            parts.append(TURN_CONTRACT_RESCHEDULE)
        if self.extra_contract:
            parts.append(self.extra_contract)
        return "\n".join(parts)

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
        return history + [user_message(self.pending_user, self.pending_images)]

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
            "s6": {
                "facts": len(self._retrieval.facts) if self._retrieval else 0,
                "episodes": len(self._retrieval.episodes) if self._retrieval else 0,
                "chars": len(blocks[5]),
                **({"error": self._retrieval_error} if self._retrieval_error else {}),
            },
            "s7": len(blocks[6]),
            "s8": len(blocks[7]),
            "history_turns": len(history) // 2,
            # 超长输入被截掉多少字（0 = 没截断）；查"为什么它没看见后半段"看这里
            "user_truncated": self.truncated_chars,
        }


def load_core_files(settings: Settings) -> dict[str, str]:
    """一次性读三文件（doctor / 调试用）。"""
    return {name: read_core_file(settings, name) for name in CORE_FILES}


def _tools_of(raw: object) -> list[str]:
    """把 ``chat_log.tools_json`` 还原成工具名列表（解析不了就返回空表，不炸回放）。"""
    if not raw:
        return []
    try:
        items = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    return [str(item.get("tool", "")) for item in items if isinstance(item, dict)]
