"""``yixiang ops explain-search "<query>"``：把检索的五个阶段摊开（§8.3）。

**检索不能是黑盒**：golden 集的分数掉下来时，没有这个工具根本不知道是 FTS、向量、
融合、过滤还是加权那一层出的问题。所以这里刻意不做美化——每段都打印这一层的
原始名次与被剔掉的理由，演示与调参都照它念。

五段对应 ``retrieve.explain_search()`` 的五个字段：
``fts`` / ``vec`` / ``fused`` / ``filtered_out`` / ``ranked``。
"""

from __future__ import annotations

from yixiang.rag import retrieve

MAX_ROWS = 10


def render_explain(explain: retrieve.SearchExplain) -> str:
    """五段齐全的终端渲染（缺一段就说明那一层没跑起来，如实标注）。"""
    filters = explain.filters or {}
    lines = [
        f"检索：{explain.query or '（空查询 → 评分兜底）'}",
        f"参数：top_k={filters.get('top_k')} · mtype={filters.get('mtype') or '不限'} · "
        f"年份 {filters.get('year_from') or '不限'}~{filters.get('year_to') or '不限'} · "
        f"去重窗口 {filters.get('exclude_recent_days')} 天 · "
        f"嵌入 {explain.embed}",
        "",
    ]
    lines += _section("① FTS5 召回（jieba 预分词 + LIKE 兜底）", explain.fts, explain)
    lines += _section(_vec_heading(explain), explain.vec, explain)
    lines += _section("③ RRF 融合（k=60，只看名次不看分数量纲）", explain.fused, explain)
    lines += _filtered_section(explain)
    lines += _ranked_section(explain)
    if explain.degraded:
        lines.append(
            "提示：嵌入不可用，本次检索是纯 FTS5 的降级结果（D-24：用户无感，"
            "trace 里记 E_EMBED_UNAVAILABLE）。"
        )
    return "\n".join(lines)


def _vec_heading(explain: retrieve.SearchExplain) -> str:
    if explain.embed == "ok":
        return "② 向量召回（sqlite-vec KNN，余弦距离）"
    return f"② 向量召回 —— 跳过（嵌入状态：{explain.embed}）"


def _section(title: str, hits: list[retrieve.MediaHit], explain: retrieve.SearchExplain) -> list[str]:
    lines = [f"{title} —— {len(hits)} 条"]
    if not hits:
        lines.append("  （空：这一路没有召回；两路都空不代表功能不可用，只是这一问没答案）")
    for index, hit in enumerate(hits[:MAX_ROWS], start=1):
        lines.append(f"  {index}. {_row(hit, explain)}")
    if len(hits) > MAX_ROWS:
        lines.append(f"  … 其余 {len(hits) - MAX_ROWS} 条省略")
    lines.append("")
    return lines


def _filtered_section(explain: retrieve.SearchExplain) -> list[str]:
    lines = [f"④ 硬过滤（近 {explain.filters.get('exclude_recent_days')} 天已推） —— 剔除 "
             f"{len(explain.filtered_out)} 条"]
    if not explain.filtered_out:
        lines.append("  （没有被剔掉的候选）")
    for hit, reason in explain.filtered_out[:MAX_ROWS]:
        lines.append(f"  - 《{hit.title}》 —— {reason}")
    if len(explain.filtered_out) > MAX_ROWS:
        lines.append(f"  … 其余 {len(explain.filtered_out) - MAX_ROWS} 条省略")
    lines.append("")
    return lines


def _ranked_section(explain: retrieve.SearchExplain) -> list[str]:
    lines = [f"⑤ 口味软加权后的最终 top-{len(explain.ranked)}（final = rrf × (1 + taste)）"]
    if not explain.ranked:
        lines.append("  （没有可交付的结果：换一个说法，或先跑 `yixiang rag ingest`）")
    for index, hit in enumerate(explain.ranked, start=1):
        lines.append(
            f"  {index}. {hit.render()}   "
            f"[rrf={hit.rrf_score:.4f} taste={hit.taste_score:+.2f} final={hit.final:.4f}]"
        )
    return lines


def _row(hit: retrieve.MediaHit, explain: retrieve.SearchExplain) -> str:
    marks = "+".join(retrieve.CHANNEL_LABELS.get(ch, ch) for ch in hit.channels)
    text = f"《{hit.title}》"
    meta = [str(hit.year)] if hit.year else []
    if hit.mtype:
        meta.append(hit.mtype)
    if hit.rating is not None:
        meta.append(f"{hit.rating:g} 分")
    if meta:
        text += "（" + " · ".join(meta) + "）"
    if marks:
        text += f" · {marks}"
    return text


def run(settings, query: str, *, top_k: int = retrieve.DEFAULT_TOP_K, **kwargs) -> int:
    """``ops explain-search`` 的命令实现：装配检索 → 渲染五段。"""
    from yixiang import db
    from yixiang.runtime.models import SystemClock
    from yixiang.tools.media import INGEST_HINT, corpus_size

    conn = db.connect(settings.db_path)
    try:
        db.migrate(conn)
        retrieve.configure(
            conn,
            data_dir=settings.data_dir,
            clock=SystemClock(),
            settings=settings,
            embedder=kwargs.get("embedder"),
        )
        if corpus_size(conn) == 0:
            print(f"影视库是空的，没有可解释的检索。\n  {INGEST_HINT}")
            return 1
        explain = retrieve.explain_search(query, top_k=top_k)
        print(render_explain(explain))
        return 0
    finally:
        retrieve.reset()
        conn.close()


__all__ = ["MAX_ROWS", "render_explain", "run"]
