"""语料与按需推荐子系统（PART 3 / TECH-DESIGN §8）。

三层职责，各管一件：

  * **入库**（``ingest.py``）——Bangumi / TMDb / 本地 JSON → ``media`` +
    ``media_fts`` + ``media_vec``；幂等键 ``source_id``，限速 + 游标续跑；
  * **检索**（``retrieve.py``）——FTS5 与向量两路召回 → ``rrf`` 融合 →
    硬过滤（近 7 天已推）→ 口味软加权 → top-k；``explain_search`` 返回五段中间结果；
  * **画像**（``taste.py``）——``user.md`` 的偏好标签 + 近 30 天推荐反馈。

与记忆子系统的边界（面试高频对比题）：记忆是写给自己的（人格 / 画像 / 情节），
RAG 是读外部的（语料库）；**共用 ``rrf`` / 预分词与嵌入纪律，但写入路径与冲突
语义完全不同**——语料是批量灌入、只有版本与覆盖，没有"人机共治"。

模块级函数是 PART 4 依赖的冻结签名（PART-3 §4），它们本身不接连接；连接与
data_dir 由组装根通过 ``configure()`` 注入，避免把签名撑变形。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from yixiang.runtime.models import Clock, SystemClock


def configure(
    conn: Any,
    *,
    data_dir: Path | str = Path("data"),
    clock: Clock | None = None,
    settings: Any = None,
    embedder: Any = None,
) -> Any:
    """装配检索上下文（由 ``App`` 调用）。嵌入后端不可用不报错，降级纯 FTS5。"""
    from yixiang.rag import retrieve

    return retrieve.configure(
        conn,
        data_dir=data_dir,
        clock=clock or SystemClock(),
        settings=settings,
        embedder=embedder,
    )


def current() -> Any:
    from yixiang.rag import retrieve

    return retrieve.current()


def is_configured() -> bool:
    from yixiang.rag import retrieve

    return retrieve.is_configured()


def reset() -> None:
    """测试用：清掉全局上下文（避免用例之间串库）。"""
    from yixiang.rag import retrieve

    retrieve.reset()


def trace_info() -> dict[str, Any]:
    """给 trace 的 ``rag`` 字段：降级状态必须可观测，但**不进** ``result.error``。"""
    from yixiang.rag import retrieve

    return retrieve.trace_info()


def clear_warnings() -> None:
    from yixiang.rag import retrieve

    retrieve.clear_warnings()


# ------------------------------------------------------------------ 冻结签名
# PART 4 按 ``from yixiang.rag import retrieve_media`` 使用（PART-3 §4）。
def retrieve_media(query: str, **kwargs: Any) -> list[Any]:
    from yixiang.rag.retrieve import retrieve_media as impl

    return impl(query, **kwargs)


def explain_search(query: str, **kwargs: Any) -> Any:
    from yixiang.rag.retrieve import explain_search as impl

    return impl(query, **kwargs)


def build_profile(conn: Any, data_dir: Path | str, **kwargs: Any) -> Any:
    from yixiang.rag.taste import build_profile as impl

    return impl(conn, data_dir, **kwargs)


def taste_score(media: Any, profile: Any) -> float:
    from yixiang.rag.taste import taste_score as impl

    return impl(media, profile)


__all__ = [
    "build_profile",
    "clear_warnings",
    "configure",
    "current",
    "explain_search",
    "is_configured",
    "reset",
    "retrieve_media",
    "taste_score",
    "trace_info",
]
