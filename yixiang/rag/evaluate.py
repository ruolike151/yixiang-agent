"""golden 集检索评测：``yixiang rag eval``（TECH §8.6 / PART-3 §7 D-11）。

口径三句话（改之前先读）：

  1. **期望集合而不是唯一答案**——推荐本来就有多解，一条查询给 1~4 部
     "算命中"的作品（§8.6 的标注方式）。top-3 命中率挂在产品指标上（≥60%）。
  2. **MRR 与命中率一起看**——命中率是"有没有"，MRR 是"排得靠不靠前"；
     调参时后者先动，所以它是指针不是门禁。
  3. **语料缺席时如实说**——没有语料 / 嵌入不可用就打印可行动的提示并退非零，
     不假装通过（这条在 PART-4 的 CI 门禁里会被复用）。

离线等价入口：``evals/fixtures/media_sample.json`` + ``HashEmbedder`` 让这条命令
在没有网络、没有模型的机器上也能跑出真实数字（PART-3 §7 离线纪律）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yixiang.rag import retrieve

# 产品指标（PRODUCT.md §1.4 / PART-3 §1）
HIT_TARGET = 0.6
DEFAULT_TOP_K = retrieve.DEFAULT_TOP_K

_TRIM = "《》「」『』\"' \t、，。!？!?."


@dataclass(slots=True)
class QueryCase:
    """golden 集里的一条查询：查询串 + 期望集合 + 备注。"""

    query: str
    expect: tuple[str, ...] = ()
    mtype: str = ""
    note: str = ""


@dataclass(slots=True)
class CaseResult:
    """一条查询的结果：返回的前 k 个标题 + 第一个命中所在的排名（0=没命中）。"""

    case: QueryCase
    top: list[str] = field(default_factory=list)
    rank: int = 0

    @property
    def hit(self) -> bool:
        return self.rank > 0


@dataclass(slots=True)
class EvalReport:
    """一次评测的报告：指标 + 明细 + 环境（语料条数、嵌入状态）。"""

    results: list[CaseResult] = field(default_factory=list)
    top_k: int = DEFAULT_TOP_K
    corpus: int = 0
    embed: str = "skipped"
    source: str = ""
    use_taste: bool = False

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def hits(self) -> int:
        return sum(1 for item in self.results if item.hit)

    @property
    def hit_rate(self) -> float:
        return self.hits / self.total if self.total else 0.0

    @property
    def mrr(self) -> float:
        """平均倒数排名：只看前 ``top_k`` 名，没命中记 0（§8.6）。"""
        if not self.total:
            return 0.0
        return sum((1.0 / item.rank if item.rank else 0.0) for item in self.results) / self.total

    def passed(self, target: float = HIT_TARGET) -> bool:
        return bool(self.total) and self.hit_rate >= target

    def misses(self) -> list[CaseResult]:
        return [item for item in self.results if not item.hit]

    def summary(self) -> str:
        lines = [
            f"检索评测（{self.source or 'golden'}）：{self.total} 条查询 · top-{self.top_k} · "
            f"语料 {self.corpus} 部 · 嵌入 {_embed_label(self.embed)} · "
            f"口味 {'开' if self.use_taste else '关（只测相关性）'}",
            f"top-{self.top_k} 命中率：{self.hit_rate * 100:.1f}%"
            f"（{self.hits}/{self.total}，目标 ≥{HIT_TARGET * 100:.0f}%）"
            f" · MRR {self.mrr:.3f}"
            f" → {'通过' if self.passed() else '未达标'}",
        ]
        for item in self.misses():
            got = "、".join(item.top) or "（无结果）"
            lines.append(
                f"  ✗ {item.case.query} —— 期望 {'/'.join(item.case.expect) or '任意'}，"
                f"实际 top-{self.top_k}：{got}"
            )
        if self.embed != "ok":
            lines.append(
                "  注意：嵌入不可用，本次是纯 FTS5 的降级指标；真实语义指标要在模型"
                "可用的机器上重跑（降级本身是 D-24 的产品要求）。"
            )
        return "\n".join(lines)


def _embed_label(state: str) -> str:
    return {"ok": "可用", "unavailable": "不可用（纯 FTS5）"}.get(state, "未使用")


# --------------------------------------------------------------------- 读取
def load_golden(path: Path | str) -> list[QueryCase]:
    """读 ``.jsonl`` golden 集：每行 ``{"query": ..., "expect": [...]}``。"""
    file_path = Path(path)
    if not file_path.is_file():
        return []
    cases: list[QueryCase] = []
    for raw in file_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        query = str(record.get("query") or "").strip()
        if not query:
            continue
        expect = record.get("expect") or record.get("expect_titles") or []
        if isinstance(expect, str):
            expect = [expect]
        cases.append(
            QueryCase(
                query=query,
                expect=tuple(str(item).strip() for item in expect if str(item).strip()),
                mtype=str(record.get("mtype") or ""),
                note=str(record.get("note") or ""),
            )
        )
    return cases


def normalize_title(text: str) -> str:
    """标题比对用的归一化：去掉书名号 / 空白 / 标点，忽略大小写。"""
    cleaned = str(text or "").strip()
    while cleaned[:1] in _TRIM:
        cleaned = cleaned[1:]
    while cleaned[-1:] in _TRIM:
        cleaned = cleaned[:-1]
    return cleaned.replace(" ", "").lower()


# --------------------------------------------------------------------- 评测
def evaluate(
    cases: list[QueryCase],
    *,
    top_k: int = DEFAULT_TOP_K,
    corpus: int = 0,
    source: str = "",
    use_taste: bool = False,
) -> EvalReport:
    """逐条检索并算指标；调用前必须先 ``rag.configure(...)``（检索层的要求）。

    ``use_taste=False`` 是**评测口径的默认**：期望集标注的是"哪些作品相关"，
    不是"这个用户喜不喜欢"。默认吃 ``user.md`` 会让同一个数字随画像变化
    （PART 4 的 L3 回归集就是 ``media.jsonl``，数字必须可比）。想看带口味
    的排序请用 ``ops explain-search`` 或 ``yixiang recommend``。
    """
    ctx = retrieve.ensure_configured()
    report = EvalReport(
        top_k=int(top_k), corpus=int(corpus), source=source, use_taste=bool(use_taste)
    )
    for case in cases:
        hits = retrieve.retrieve_media(
            case.query,
            top_k=int(top_k),
            mtype=case.mtype or None,
            exclude_recent_days=0,  # 评测不是推荐：去重窗口会把答案挡在门外
            use_taste=bool(use_taste),
        )
        titles = [hit.title for hit in hits]
        report.results.append(CaseResult(case=case, top=titles, rank=_rank(titles, case.expect)))
    report.embed = ctx.embed
    return report


def _rank(titles: list[str], expect: tuple[str, ...]) -> int:
    wanted = {normalize_title(item) for item in expect if item}
    if not wanted:
        return 0
    for index, title in enumerate(titles, start=1):
        if normalize_title(title) in wanted:
            return index
    return 0


def corpus_size(conn: Any) -> int:
    """``media`` 表里的作品数（语料缺席时评测没有意义，先看这个数）。"""
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM media").fetchone()
    except Exception:  # 还没迁移的库
        return 0
    return int(row["n"]) if row else 0


__all__ = [
    "DEFAULT_TOP_K",
    "HIT_TARGET",
    "CaseResult",
    "EvalReport",
    "QueryCase",
    "corpus_size",
    "evaluate",
    "load_golden",
    "normalize_title",
]
