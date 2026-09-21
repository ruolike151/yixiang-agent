"""真实路径：真网络 + 真文件系统，**不进 PR 门禁**（TECH §13.6）。

跑法（仓库根，PowerShell）：

    $env:PYTHONIOENCODING="utf-8"
    .venv\\Scripts\\python.exe -m pytest evals/live -o addopts= -q -m live -rs

为什么不塞进 ``evals/deterministic``：这一组会**出网**、**下载模型**、
**写真实 ``data/``**，而那一组的纪律是"离线、零成本、30 秒内跑完"。
``pyproject.toml`` 的 ``testpaths = ["evals"]`` 会把两个目录一起收集，
默认的 ``-m "not live"`` 负责把它们全部 deselected。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yixiang import db
from yixiang.rag import ingest


@pytest.mark.live
def test_bangumi_really_answers_and_lands_in_data_raw(live_data_dir: Path):
    """真抓一页：Bangumi 得回条目，原始 JSON 得落进 ``data/raw/``。"""
    items = ingest.fetch_bangumi(data_dir=live_data_dir, pages=1)

    assert items, "Bangumi 返回空列表：先确认网络 / 接口可达，再谈入库"
    assert all(item.source_id.startswith("bangumi:") for item in items)
    assert all(item.title for item in items)
    cached = sorted((live_data_dir / ingest.RAW_DIRNAME).glob("bangumi-*.json"))
    assert cached, "原始响应没有落进 data/raw：缓存层坏了，出网成本会翻倍"


@pytest.mark.live
def test_the_same_page_ingested_twice_is_all_skipped(live_data_dir: Path, tmp_path: Path):
    """抓一次 → 入库 → 同样的数据再入库一次：第二遍必须全是"跳过"。"""
    from yixiang.rag.embed import HashEmbedder

    items = ingest.fetch_bangumi(data_dir=live_data_dir, pages=1)
    conn = db.connect(tmp_path / "live.db")
    try:
        db.migrate(conn)
        embedder = HashEmbedder()
        first = ingest.ingest_items(conn, items, source="bangumi", embedder=embedder)
        second = ingest.ingest_items(conn, items, source="bangumi", embedder=embedder)
    finally:
        conn.close()

    assert first.inserted == len(items)
    assert second.skipped == len(items)
    assert second.inserted == 0
