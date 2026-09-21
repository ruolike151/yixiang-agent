"""嵌入口径：文档写的模型 / 维度必须与代码常量一致（TECH §8.2、§980-983）。

真嵌入跑起来之后，"文档里的 512 维 bge-small-zh"与"代码里的 DEFAULT_MODEL /
DEFAULT_DIM"就成了同一件事的两份副本。这个文件负责让它们不许分叉：
换了模型不改文档，或者改了文档不改代码，都必须在 PR 上红。
"""

from __future__ import annotations

from pathlib import Path

from yixiang.rag.embed import (
    BUILTIN_DIM,
    DEFAULT_DIM,
    DEFAULT_MODEL,
    EMBED_DIM_META,
    EMBED_MODEL_META,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NUMBERS = REPO_ROOT / "docs" / "NUMBERS.md"


def test_the_default_model_is_the_one_the_numbers_card_names():
    assert DEFAULT_MODEL == "BAAI/bge-small-zh-v1.5"
    text = NUMBERS.read_text(encoding="utf-8")
    assert "bge-small-zh-v1.5" in text  # 数字卡必须写明是哪张嵌入算出来的


def test_the_default_dimension_matches_the_builtin_table():
    assert DEFAULT_DIM == 512
    assert BUILTIN_DIM[DEFAULT_MODEL] == DEFAULT_DIM
    assert "512" in NUMBERS.read_text(encoding="utf-8")


def test_the_numbers_card_says_which_backend_produced_each_row():
    """每一行检索数字都得能看出来源：``hash`` 是假嵌入，真机跑的是 fastembed。"""
    text = NUMBERS.read_text(encoding="utf-8")

    assert "YIXIANG_EMBED_BACKEND=hash" in text  # 离线/CI 那一档的口径
    assert "fastembed" in text  # 真嵌入那一档的口径（Task 8 才补上的行）


def test_the_vector_metadata_keys_are_the_frozen_names():
    """换名 = 老库读不出 meta = 静默跨语义空间比分数，所以这两个键钉死。"""
    assert EMBED_MODEL_META == "media.embed_model"
    assert EMBED_DIM_META == "media.embed_dim"


def test_the_model_cache_is_the_path_the_docs_promise():
    """文档承诺 ``~/.cache/fastembed``（TECH §1884、``.env.example``）。

    fastembed 的默认缓存目录其实是系统临时目录——它会被磁盘清理删掉，删掉之后
    离线环境只能降级成纯 FTS5。所以代码必须显式指定路径，不能吃默认值。
    """
    from yixiang.rag.embed import CACHE_DIR

    promised = Path.home() / ".cache" / "fastembed"
    assert promised == CACHE_DIR
    assert "temp" not in str(CACHE_DIR).lower()
