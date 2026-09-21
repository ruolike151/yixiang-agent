"""``evals/live`` 自己的夹具。

仓库根**没有**顶层 ``conftest.py``，``evals/deterministic/conftest.py`` 只作用于
它自己那棵子树——live 用例要用什么夹具，必须在这里自带一份（否则
``repo_root`` 直接报 fixture not found）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def live_data_dir(repo_root: Path) -> Path:
    """真抓取 / 真嵌入的落点：默认仓库的 ``data/``，可用环境变量换到别处。"""
    raw = os.environ.get("YIXIANG_LIVE_DATA_DIR", "").strip()
    return Path(raw).resolve() if raw else repo_root / "data"
