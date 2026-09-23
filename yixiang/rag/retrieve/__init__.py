"""``yixiang.rag.retrieve`` 包门面（Task 24：从 924 行的单文件拆出来）。

对外**逐字保持**原来的 ``yixiang.rag.retrieve``：``rag/__init__.py`` 的十个转发函数与
七个调用点（app / tools.brief / tools.media / ops.explain_search / ops.rag_cmd /
rag.evaluate / rag.ingest）一行都不用改。内部按职责分：

    models.py   数据载体（MediaHit / SearchExplain / 摘要片段）
    context.py  全局装配与这一轮的状态（configure / reset / trace_info）
    text.py     冻结文本与分词（embed_text_for / fts_tokens / 重建 FTS）
    vector.py   嵌入与向量索引（embed_texts 带超时 / ensure_media_vec / reindex）
    search.py   四路召回与浏览（search_fts / search_like / search_vec / browse_media）
    rerank.py   重排、解释与推荐留痕（explain_search / retrieve_media / log_recommendation）

依赖是单向的：``models ← context/text ← vector ← search ← rerank``。**别合回去**——
拆包不是为了好看，是给"嵌入超时"找一个明确的落点（见 Task 24）。

``__all__`` 逐字保持旧 ``retrieve.py`` 的写法，所以 CHANNEL_LABELS / TOKENIZER_VERSION /
VEC_MEDIA 不在里面；但这三个包外按属性取用（``ops/explain_search.py``、
``ops/rag_cmd.py``、``rag/ingest/store.py``），于是下面用 ``x as x`` 显式再导出一次。
"""

from __future__ import annotations

from yixiang.memory.semantic import CANDIDATE_LIMIT
from yixiang.rag.retrieve.context import (
    RetrievalContext,
    clear_warnings,
    configure,
    current,
    ensure_configured,
    is_configured,
    reset,
    trace_info,
)
from yixiang.rag.retrieve.models import MediaHit, SearchExplain, synopsis_snippet
from yixiang.rag.retrieve.rerank import (
    DEFAULT_EXCLUDE_RECENT_DAYS,
    DEFAULT_TOP_K,
    explain_search,
    log_recommendation,
    retrieve_media,
)
from yixiang.rag.retrieve.search import (
    CHANNEL_LABELS as CHANNEL_LABELS,
)
from yixiang.rag.retrieve.search import (
    browse_media,
    search_fts,
    search_like,
    search_vec,
)
from yixiang.rag.retrieve.text import (
    TOKENIZER_VERSION as TOKENIZER_VERSION,
)
from yixiang.rag.retrieve.text import (
    embed_text_for,
    fts_index_row,
    fts_tokens,
    fts_unindex_row,
    normalize_mtype,
    rebuild_media_fts,
    row_embed_text,
)
from yixiang.rag.retrieve.vector import (
    VEC_MEDIA as VEC_MEDIA,
)
from yixiang.rag.retrieve.vector import (
    embed_texts,
    ensure_media_vec,
    index_media_vectors,
    rebuild_media_vec,
    reindex_media,
    reset_media_vec,
    write_media_vectors,
)

__all__ = [
    "CANDIDATE_LIMIT",
    "DEFAULT_EXCLUDE_RECENT_DAYS",
    "DEFAULT_TOP_K",
    "MediaHit",
    "RetrievalContext",
    "SearchExplain",
    "browse_media",
    "clear_warnings",
    "configure",
    "current",
    "embed_text_for",
    "embed_texts",
    "ensure_configured",
    "ensure_media_vec",
    "explain_search",
    "fts_index_row",
    "fts_tokens",
    "fts_unindex_row",
    "index_media_vectors",
    "is_configured",
    "log_recommendation",
    "normalize_mtype",
    "rebuild_media_fts",
    "rebuild_media_vec",
    "reindex_media",
    "reset",
    "reset_media_vec",
    "retrieve_media",
    "row_embed_text",
    "search_fts",
    "search_like",
    "search_vec",
    "synopsis_snippet",
    "trace_info",
    "write_media_vectors",
]
