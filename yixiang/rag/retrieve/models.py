"""检索的数据载体（Task 24 从 924 行的单文件拆出）。

``MediaHit``（一条命中，``render`` 与 ``as_trace`` 都在这里）与 ``SearchExplain``
（五段中间结果，``degraded`` 就是 ``embed != "ok"``）。简介摘要 ``synopsis_snippet``
跟着 ``MediaHit.render`` 走：它是不可信外部文本，必须与包裹纪律同处一室。
这一层不 import 任何同包模块，是全包的底座。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 简介摘要长度：够显示"这条为什么像"的语义线索，又不至于把 top-3 撑成一篇长文
SYNOPSIS_CHARS = 80


def synopsis_snippet(synopsis: str, limit: int = SYNOPSIS_CHARS) -> str:
    """摘一段简介用于展示：空白压平、超长截断加省略号。

    这段文本是**不可信的外部内容**（T-2 / D-23），只允许经
    ``tools.media.wrap_external`` 进 prompt——所以它必须跟着 ``MediaHit.render()``
    一起出现，否则"包裹住不可信文本"这条纪律就没有实物可验。
    """
    text = " ".join(str(synopsis or "").split())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


# --------------------------------------------------------------------- 数据结构
@dataclass(slots=True)
class MediaHit:
    """一条影视命中（PART-3 §4 的冻结字段在前，其余是实现细节）。

    ``reason`` 必须能引用命中字段（"为什么是它"是产品指标的一部分）：演示时
    照着念就是一段可核对的理由，而不是"模型觉得像"。
    """

    id: int
    title: str
    year: int | None = None
    mtype: str = ""
    rating: float | None = None
    genres: list[str] = field(default_factory=list)
    rrf_score: float = 0.0
    taste_score: float = 0.0
    final: float = 0.0
    reason: str = ""
    source_id: str = ""
    synopsis: str = ""
    channels: list[str] = field(default_factory=list)

    def render(self) -> str:
        """可核对的展示文本：首行字段（标题 / 年份 / 类型 / 评分 / 标签 / 理由），

        次行是简介摘要（不可信文本，进 prompt 前由调用方包裹，见 ``synopsis_snippet``）。
        """
        meta = [str(self.year)] if self.year else []
        if self.mtype:
            meta.append(self.mtype)
        if self.rating is not None:
            meta.append(f"{self.rating:g} 分")
        text = f"《{self.title}》"
        if meta:
            text += "（" + " · ".join(meta) + "）"
        if self.genres:
            text += " · " + "、".join(self.genres)
        if self.reason:
            text += f" —— {self.reason}"
        snippet = synopsis_snippet(self.synopsis)
        if snippet:
            text += f"\n   简介：{snippet}"
        return text

    def as_trace(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "year": self.year,
            "mtype": self.mtype,
            "rating": self.rating,
            "rrf": round(self.rrf_score, 4),
            "taste": round(self.taste_score, 4),
            "final": round(self.final, 4),
            "reason": self.reason,
            "channels": list(self.channels),
        }


@dataclass(slots=True)
class SearchExplain:
    """``explain_search`` 的返回：五段中间结果齐全（§8.3，检索不是黑盒）。"""

    query: str
    fts: list[MediaHit] = field(default_factory=list)
    vec: list[MediaHit] = field(default_factory=list)
    fused: list[MediaHit] = field(default_factory=list)
    filtered_out: list[tuple[MediaHit, str]] = field(default_factory=list)
    ranked: list[MediaHit] = field(default_factory=list)
    embed: str = "skipped"
    filters: dict[str, Any] = field(default_factory=dict)
    profile: Any = None

    @property
    def degraded(self) -> bool:
        """这一问是不是降级答的（embed 没真正跑起来）。"""
        return self.embed != "ok"
