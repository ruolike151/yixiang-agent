"""口味画像：``build_profile()`` + ``taste_score()``（TECH §8.5）。

两个信号源，都是"已经有的事实"而不是新造的：

  * ``data/user.md`` 的 ``## 偏好`` 段 —— 人（或 ``update_user`` 工具）写下的
    喜欢 / 不喜欢类型词；
  * ``recommend_log.feedback`` —— 用户对已推作品的反馈（good / bad / none）。

**软加权而不是硬过滤**（§8.5 的设计点）：硬过滤会让推荐收敛到回声室，
``final = rrf_score * (1 + taste_score)`` 里 ``±0.5`` 的上限保证"相关性"
始终是主信号，口味只做微调。冷启动（还没有任何画像信号）时 ``taste_score``
恒为 0，退化成纯相关性排序——这也是产品上最容易解释的默认行为。

故意的简化（写在这里免得以后当成 bug）：§8.5 说反馈还可以来自"对话里说
'这部我看过，一般'经巩固进 episodes"，那要等 PART 4 的巩固口径稳定后再接；
现在只读 ``recommend_log``，口径单一、可断言。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from yixiang.runtime.models import Clock, SystemClock

# 权重常数（§8.5 的公式，改这里就是改产品口径）
LIKED_GAIN = 0.15
DISLIKED_PENALTY = 0.25
RECENT_GOOD_GAIN = 0.20
RECENT_BAD_PENALTY = 0.30

# 软加权的上下限：±50%，相关性仍是主信号
TASTE_MIN = -0.5
TASTE_MAX = 0.5

# 反馈统计窗口：近 30 天（§8.5）
FEEDBACK_WINDOW_DAYS = 30

# ``## 偏好`` 段里的书写约定（templates/user.md 里写着同一份）
LIKED_MARKERS = ("喜欢：", "喜欢:")
DISLIKED_MARKERS = ("不喜欢：", "不喜欢:")

# 类型词之间的分隔符：中文顿号 / 逗号 / 分号 / 斜杠 / 空格
_TAG_SPLIT = re.compile(r"[、,，;；/｜|\s]+")
# 旧写法（"喜欢悬疑、科幻题材"）里要裁掉的尾巴
_TAIL_NOISE = ("题材", "类型", "片子", "电影", "番剧", "动漫", "的作品", "类")
_SECTION_RE = re.compile(r"^##\s*(.+?)\s*$")


@dataclass(slots=True)
class TasteProfile:
    """一次画像快照：两个标签集合 + 两个近期反馈占比。"""

    liked_tags: set[str] = field(default_factory=set)
    disliked_tags: set[str] = field(default_factory=set)
    recent_good_ratio: float = 0.0
    recent_bad_ratio: float = 0.0
    cold_start: bool = False
    sources: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.liked_tags or self.disliked_tags) and not (
            self.recent_good_ratio or self.recent_bad_ratio
        )


def build_profile(
    conn: sqlite3.Connection,
    data_dir: Path | str,
    *,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> TasteProfile:
    """装配画像：``user.md`` 的偏好 + 近 30 天的推荐反馈（§8.5）。"""
    moment = now or (clock or SystemClock()).now()
    profile = TasteProfile()
    _read_user_preferences(Path(data_dir) / "user.md", profile)
    _read_feedback(conn, profile, moment)
    profile.cold_start = profile.is_empty()
    return profile


def taste_score(media: Any, profile: TasteProfile) -> float:
    """``w = Σ 标签 ± 权重 + 近期反馈占比``，clamp 到 ``[-0.5, +0.5]``（§8.5）。

    ``media`` 允许是 ``MediaHit`` / ``sqlite3.Row`` / ``dict``——检索层与
    explain 工具都要能直接调它，别为了一个 getter 造第三套数据结构。
    """
    genres = _genres_of(media)
    weight = 0.0
    for tag in genres:
        if tag in profile.liked_tags:
            weight += LIKED_GAIN
        if tag in profile.disliked_tags:
            weight -= DISLIKED_PENALTY
    weight += RECENT_GOOD_GAIN * profile.recent_good_ratio
    weight -= RECENT_BAD_PENALTY * profile.recent_bad_ratio
    return clamp(weight)


def clamp(value: float, low: float = TASTE_MIN, high: float = TASTE_MAX) -> float:
    return max(low, min(high, value))


def _genres_of(media: Any) -> list[str]:
    """把三种可能的载体统一成 ``list[str]``（缺字段按空列表处理，不抛异常）。"""
    raw: Any
    if isinstance(media, dict):
        raw = media.get("genres")
    elif hasattr(media, "genres"):
        raw = media.genres
    else:
        try:
            raw = media["genres"]
        except (KeyError, IndexError, TypeError):
            return []
    return split_genres(raw)


def split_genres(raw: Any) -> list[str]:
    """``"悬疑/科幻"`` 或 ``["悬疑", "科幻"]`` → 干净的标签列表。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = [str(item) for item in raw]
    else:
        items = _TAG_SPLIT.split(str(raw))
    return [tag for tag in (item.strip() for item in items) if tag]


# --------------------------------------------------------------------- 读取
def _read_user_preferences(path: Path, profile: TasteProfile) -> None:
    """只读 ``## 偏好`` 段：其他分区（身份 / 作息 / 约束）与口味无关。"""
    if not path.is_file():
        return
    inside = False
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = _SECTION_RE.match(line.strip())
        if heading:
            inside = heading.group(1).strip() == "偏好"
            continue
        if not inside:
            continue
        text = line.strip().lstrip("-*").strip()
        if not text or text.startswith("<!--") or text.startswith("//"):
            continue
        liked, disliked = parse_preference_line(text)
        if liked or disliked:
            profile.sources.append(text)
        profile.liked_tags.update(liked)
        profile.disliked_tags.update(disliked)


def parse_preference_line(text: str) -> tuple[list[str], list[str]]:
    """一行偏好文本 → ``(liked, disliked)``。

    承认两种写法：

    * **约定写法** ``喜欢：悬疑、科幻`` / ``不喜欢：恐怖``（templates/user.md 里教的）；
    * **旧写法** ``喜欢悬疑、科幻题材；日常番是轻度观众。``——按"喜欢"后的第一个
      分句取标签，并把 ``题材 / 类型`` 这类尾巴裁掉。

    解析器写得宽容是刻意的：画像是软加权，偶尔多认一个标签只是排序微调；
    但**不能因为一行写得不标准就让收益整个消失**。
    """
    disliked: list[str] = []
    liked: list[str] = []
    for segment in _split_clauses(text):
        if any(marker in segment for marker in DISLIKED_MARKERS):
            disliked.extend(_tags_after(segment, DISLIKED_MARKERS))
        elif "喜欢" in segment:
            liked.extend(_tags_after(segment, LIKED_MARKERS) or _legacy_tags(segment))
    return _dedupe(liked), _dedupe(disliked)


def _split_clauses(text: str) -> list[str]:
    return [part for part in re.split(r"[；;。]", text) if part.strip()]


def _tags_after(segment: str, markers: tuple[str, ...]) -> list[str]:
    for marker in markers:
        if marker in segment:
            return _clean_tags(segment.split(marker, 1)[1])
    return []


def _legacy_tags(segment: str) -> list[str]:
    """``喜欢悬疑、科幻题材`` → ``["悬疑", "科幻"]``（没有冒号的旧写法）。"""
    tail = segment.split("喜欢", 1)[1]
    for noise in ("是", "，", ","):
        tail = tail.split(noise, 1)[0]
    return _clean_tags(tail)


def _clean_tags(raw: str) -> list[str]:
    tags = []
    for tag in _TAG_SPLIT.split(raw):
        cleaned = _strip_tail(tag)
        if cleaned and not cleaned.startswith(("http", "//")):
            tags.append(cleaned)
    return tags


def _strip_tail(tag: str) -> str:
    cleaned = tag.strip().strip("。、，,；;！!？?()（）[]【】")
    changed = True
    while changed:
        changed = False
        for noise in _TAIL_NOISE:
            if len(cleaned) > len(noise) and cleaned.endswith(noise):
                cleaned = cleaned[: -len(noise)]
                changed = True
    return cleaned


def _dedupe(tags: list[str]) -> list[str]:
    return list(dict.fromkeys(tag for tag in tags if 1 <= len(tag) <= 12))


def _read_feedback(conn: sqlite3.Connection, profile: TasteProfile, now: datetime) -> None:
    """近 30 天 ``recommend_log`` 里 good / bad 的占比（§8.5）。"""
    since = (now - timedelta(days=FEEDBACK_WINDOW_DAYS)).date().isoformat()
    try:
        rows = conn.execute(
            "SELECT feedback, COUNT(*) AS n FROM recommend_log "
            "WHERE recommended_on >= ? AND feedback IN ('good', 'bad') GROUP BY feedback",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:  # 还没迁移过的库：画像退化为空，不炸
        return
    counts = {str(row["feedback"]): int(row["n"]) for row in rows}
    total = counts.get("good", 0) + counts.get("bad", 0)
    if total:
        profile.recent_good_ratio = counts.get("good", 0) / total
        profile.recent_bad_ratio = counts.get("bad", 0) / total
        profile.sources.append(f"近 {FEEDBACK_WINDOW_DAYS} 天反馈 {total} 条")


__all__ = [
    "DISLIKED_PENALTY",
    "LIKED_GAIN",
    "TASTE_MAX",
    "TASTE_MIN",
    "TasteProfile",
    "build_profile",
    "clamp",
    "parse_preference_line",
    "split_genres",
    "taste_score",
]
