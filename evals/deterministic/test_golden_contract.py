"""golden 集冻结契约（PART-4 §4：只增不改）。

这三条不是"测检索准不准"（那是 test_retrieval.py 的活），而是**防止考卷被悄悄改**：
条数、两条已知 MISS、以及"它们为什么还在"的决策文档，都得同时成立。
"""

from __future__ import annotations

import json
from pathlib import Path

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "media.jsonl"
DECISIONS = Path(__file__).resolve().parents[2] / "docs" / "golden-decisions.md"
KNOWN_MISS = ("名字里带夏天的动画", "宫崎骏的龙猫")


def _cases() -> list[dict]:
    raw = GOLDEN.read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def test_the_golden_set_is_still_the_frozen_twenty_cases():
    assert len(_cases()) == 20


def test_the_two_known_misses_are_still_in_the_set_and_carry_a_reason():
    by_query = {case["query"]: case for case in _cases()}

    for query in KNOWN_MISS:
        assert query in by_query
        assert by_query[query]["note"].strip()  # 缺口必须带理由，不许留空


def test_the_decision_for_every_known_miss_is_written_down():
    text = DECISIONS.read_text(encoding="utf-8")

    for query in KNOWN_MISS:
        assert query in text
    assert "只增不改" in text  # 决策文档要复述这条纪律
