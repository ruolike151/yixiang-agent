"""嵌入后端：``Embedder`` 协议 + fastembed 实现 + ``embedding_cache``（TECH §8.2）。

与 PART 2 ``memory/semantic.py`` 的 ``Embedder`` 有意**不是同一个协议**：
那边是 ``name`` / ``embed()``（facts 的短文本），这边是 ``model`` / ``dim`` /
``encode(texts, batch)``（语料批量入库，需要 batch 与维度自检）。两者共享的是
"不可用就抛 ``EmbedUnavailable``、由检索层降级"这条纪律，不是同一段代码。

三条硬约束（改之前先读）：

  1. **默认路径不引入 torch**（§8.2）：生产后端是 fastembed（onnxruntime）。
     ``sentence-transformers`` / ``api`` 在配置里留了位置但**没有实现**——没实现
     就抛 ``EmbedUnavailable`` 降级为纯 FTS5，不装作能跑（D-24 的降级路径）。
  2. **懒加载**：模型文件只在第一次 ``encode`` 时加载。没下模型的机器照样能跑
     纯关键词检索的确定性用例，也不会让 ``App`` 启动就崩。
  3. **嵌入结果进 ``embedding_cache``**：重复入库不重算（§8.1 幂等策略的省钱
     关键）；缓存键里带模型名，换模型不会读到旧向量。
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import struct
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from yixiang.runtime.models import to_local_iso

# 向量维度的元数据键：与记忆的 ``memory.vec_dim`` 分开命名，互不干扰
EMBED_MODEL_META = "media.embed_model"
EMBED_DIM_META = "media.embed_dim"

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_DIM = 512

# 模型缓存目录：**必须显式指定**。fastembed 的默认值是系统临时目录
# （``%TEMP%\fastembed_cache``），会被磁盘清理删掉；删掉之后离线机器只能降级成
# 纯 FTS5，而重下一次是 100MB。文档（TECH §1884、``.env.example``）承诺的就是
# 这个路径，代码与文档在这里不许分叉。
CACHE_DIR = Path.home() / ".cache" / "fastembed"

# 已知模型的维度（省掉"先嵌入一条才知道维度"的启动开销）
BUILTIN_DIM = {DEFAULT_MODEL: 512, "BAAI/bge-base-zh-v1.5": 768}


class EmbedUnavailable(Exception):
    """嵌入后端不可用（没装依赖 / 模型没下下来 / 后端没实现）。

    上层捕获它并降级为纯 FTS5——**宁可检索质量降级，不能让功能不可用**（D-24）。
    """


class ReindexRequired(RuntimeError):
    """向量维度或模型与库里的元数据对不上：旧向量全部作废，必须重建（§8.2）。

    这种时候静默混用会得到"看起来能跑、排名全是乱的"——比直接失败更糟。
    """


class Embedder(Protocol):
    """PART 3 冻结的嵌入协议（PART-3 §4，PART 4 按此建回归）。"""

    model: str
    dim: int

    def encode(self, texts: list[str], batch: int = 32) -> list[list[float]]: ...


# --------------------------------------------------------------------- 后端
_TOKEN_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]+")


def _bigrams(text: str) -> list[str]:
    """词袋：jieba 词 + 单字 + 相邻字对（中文相似度靠字对撑起来）。

    与 ``memory/semantic.py`` 的 ``_bigrams`` 同源——它是"离线可复现"的关键，
    也是确定性用例里 HashEmbedder 能替代真模型的原因（§13.6）。
    """
    from yixiang.memory.semantic import _bigrams as shared

    return shared(text)


class HashEmbedder:
    """确定性哈希词袋嵌入：同文本余弦 1.0，字面相近的文本余弦高。

    存在的唯一理由：让检索管线（RRF / 过滤 / 口味加权 / 去重）在没有网络、
    没有模型的机器上也能被断言。生产默认走 fastembed。
    """

    def __init__(self, model: str = "hash", dim: int = DEFAULT_DIM) -> None:
        self.model = model
        self.dim = dim

    def encode(self, texts: list[str], batch: int = 32) -> list[list[float]]:  # noqa: ARG002
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in _bigrams(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] % 2 else -1.0
            vector[index] += sign
        return _l2_normalize(vector)


class FastEmbedEmbedder:
    """fastembed（onnxruntime）后端——生产默认，**懒加载**（§8.2）。

    ``dim`` 先取已知模型表；表里没有的模型在第一次 ``encode`` 后补上实际维度，
    这样"换个 bge 变体"不需要改代码。
    """

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.model = model
        self._model_name = model
        self._backend: Any = None
        self.dim = BUILTIN_DIM.get(model, 0)

    def _load(self) -> Any:
        if self._backend is None:
            try:
                from fastembed import TextEmbedding  # type: ignore import-not-found

                self._backend = TextEmbedding(
                    model_name=self._model_name, cache_dir=str(CACHE_DIR)
                )
            except Exception as exc:  # 模型没下下来 / onnxruntime 缺库
                raise EmbedUnavailable(f"fastembed 不可用：{exc}") from exc
        return self._backend

    def encode(self, texts: list[str], batch: int = 32) -> list[list[float]]:
        if not texts:
            return []
        backend = self._load()
        try:
            vectors = [
                [float(value) for value in vector]
                for vector in backend.embed(list(texts), batch_size=max(int(batch), 1))
            ]
        except Exception as exc:
            raise EmbedUnavailable(f"嵌入失败：{exc}") from exc
        if vectors and not self.dim:
            self.dim = len(vectors[0])
        return vectors


def build_embedder(settings: Any = None) -> Embedder:
    """按 ``Settings.embed_backend`` 选后端；不可用时抛 ``EmbedUnavailable``。

    ``hash`` 是测试 / 离线用的假后端（``YIXIANG_EMBED_BACKEND=hash``）：
    它让"入库 → 检索 → 推荐"整条链路在没有模型的机器上可复现。
    """
    backend = str(getattr(settings, "embed_backend", "") or "fastembed").strip().lower()
    model = str(getattr(settings, "embed_model", "") or DEFAULT_MODEL).strip()
    if backend in {"hash", "none"}:
        return HashEmbedder()
    if backend == "fastembed":
        return FastEmbedEmbedder(model)
    # sentence-transformers / api：配置项留着，实现没写——如实降级
    raise EmbedUnavailable(f"嵌入后端 {backend!r} 尚未实现（当前支持 fastembed / hash）")


# ----------------------------------------------------------------- 向量编解码
def to_blob(vector: list[float]) -> bytes:
    """float32 小端 BLOB——sqlite-vec 的 ``vec0`` 与 ``embedding_cache`` 共用。"""
    return struct.pack(f"<{len(vector)}f", *vector)


def from_blob(blob: bytes) -> list[float]:
    if not blob:
        return []
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


# ------------------------------------------------------------------- 缓存
def content_hash(text: str, model: str) -> str:
    """缓存键：**模型名参与哈希**，否则换模型会读到旧语义空间里的向量。"""
    payload = f"{model}\x1f{text}".encode()
    return hashlib.sha256(payload).hexdigest()


def cached_vectors(
    conn: sqlite3.Connection, texts: list[str], model: str
) -> dict[str, list[float]]:
    """批量取缓存：返回 ``{content_hash: vector}``（未命中的键不出现）。"""
    if not texts:
        return {}
    hashes = {content_hash(text, model) for text in texts}
    found: dict[str, list[float]] = {}
    rows = conn.execute(
        "SELECT content_hash, vector FROM embedding_cache WHERE model = ?", (model,)
    ).fetchall()
    for row in rows:
        key = str(row["content_hash"])
        if key in hashes:
            found[key] = from_blob(row["vector"])
    return found


def store_vectors(
    conn: sqlite3.Connection,
    entries: list[tuple[str, list[float]]],
    *,
    model: str,
    dim: int,
    created_at: str | None = None,
) -> int:
    """写缓存（同键覆盖）。``entries`` 是 ``(content_hash, vector)`` 列表。"""
    if not entries:
        return 0
    stamp = created_at or to_local_iso(_now())
    with conn:
        conn.executemany(
            "INSERT INTO embedding_cache(content_hash, model, dim, vector, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(content_hash) DO UPDATE SET model = excluded.model, "
            "dim = excluded.dim, vector = excluded.vector, created_at = excluded.created_at",
            [
                (key, model, int(dim), to_blob(vector), stamp)
                for key, vector in entries
                if vector
            ],
        )
    return len(entries)


def embed_with_cache(
    conn: sqlite3.Connection,
    embedder: Embedder,
    texts: list[str],
    *,
    batch: int = 32,
    created_at: str | None = None,
    timeout: float | None = None,
) -> list[list[float]]:
    """带缓存的批量嵌入；``embedder`` 不可用时抛 ``EmbedUnavailable``（上层降级）。

    这是幂等入库的核心：``source_id`` 没变 → ``embed_text`` 没变 → 哈希命中 →
    **一次模型推理都不做**（§8.1）。

    ``timeout`` 只包住 ``encode`` 这一段（缓存读写留在调用线程，见
    ``_run_with_timeout``）；``None`` = 老行为，不设上限。
    """
    if not texts:
        return []
    cache = cached_vectors(conn, texts, embedder.model)
    missing = [text for text in texts if content_hash(text, embedder.model) not in cache]
    if missing:
        if timeout is None:
            vectors = embedder.encode(missing, batch=batch)
        else:
            vectors = _run_with_timeout(timeout, embedder.encode, missing, batch=batch)
        if len(vectors) != len(missing):
            raise EmbedUnavailable(f"嵌入数量不匹配：期望 {len(missing)}，得到 {len(vectors)}")
        store_vectors(
            conn,
            [
                (content_hash(text, embedder.model), vector)
                for text, vector in zip(missing, vectors, strict=False)
            ],
            model=embedder.model,
            dim=embedder.dim or len(vectors[0]),
            created_at=created_at,
        )
        cache.update(
            {
                content_hash(text, embedder.model): vector
                for text, vector in zip(missing, vectors, strict=False)
            }
        )
    return [cache[content_hash(text, embedder.model)] for text in texts]


def _now():
    from datetime import datetime

    return datetime.now().astimezone()


def _run_with_timeout(
    timeout: float, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """在 ``timeout`` 秒内等 ``fn`` 的结果；超时抛 ``EmbedUnavailable``。

    为什么是裸线程而不是 ``ThreadPoolExecutor``：``concurrent.futures`` 在解释器退出时
    会逐个 join 工作线程（``_python_exit``），后端真卡住时 pytest 收尾会被那条线拖住——
    超时等于白做。守护线程不会被 join，卡住的那条线随进程一起走。

    线程里**只跑 ``fn``**：``sqlite3.Connection`` 默认 ``check_same_thread=True``
    （``db.connect`` 没关它），把 conn 带进别的线程会当场 ``ProgrammingError``。
    所以缓存读写留在调用线程，线程里只有模型推理这一段。
    """
    box: list[Any] = []
    error: list[BaseException] = []

    def run() -> None:
        try:
            box.append(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 —— 后端自己抛的异常照样带回来（D-24 的降级入口）
            error.append(exc)

    worker = threading.Thread(target=run, name="yixiang-embed", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise EmbedUnavailable(f"嵌入超时（>{timeout:g}s，多半是首载模型或后端卡死）")
    if error:
        raise error[0]
    return box[0]
