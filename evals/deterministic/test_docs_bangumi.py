"""Bangumi 接入的文档口径：把会漂移的句子钉成断言。

Task 26/27/28 之后有三样东西会漂：注册表里的**工具数**（16 → 19 → 21 → 22）、TECH §9.2 的
**工具清单**（少了三个 Bangumi 工具，也没写 `read_file` / 两个 `reschedule_*`）、**数据边界**里
"对话阶段也会出网"这一行（live 检索是新的出网面）。这份用例只钉这几处。

分工：doctor 项数 / 门禁项数 / 前端栏数 / 卡 3 数字的用例在
``test_docs_consistency.py``（Task 25）；两份各管一段，不互相抄——同一句话被两份
用例同时断言，改一处就要改两个地方，那是把维护成本翻倍而不是把口径钉牢。

扫描范围是**当前形态文档**；`docs/parts/` 与 `docs/PRODUCT.md` 是交付当时的基线
快照，不回改也不参与断言（理由与 Task 25 一致）。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

TECH = "docs/TECH-DESIGN.md"

# 会写"第 N 个工具 / 共 N 个"的文件
TOOL_COUNT_DOCS = ("README.md", "docs/architecture.md", "docs/HANDOFF.md")

# 数据边界表所在的三份文档（README 与 TECH §14.4 是表本身，architecture 是转述）
BOUNDARY_DOCS = ("README.md", TECH, "docs/architecture.md")


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _registered_tools() -> list[str]:
    """从注册表源码里数工具：`Tool(name="...")` 一行一个，不必起 Settings。"""
    return re.findall(r'name="([a-z_]+)"', _read("yixiang/tools/registry.py"))


def _tech_tool_table() -> str:
    text = _read(TECH)
    start = text.index("### 9.2 工具清单")
    return text[start : text.index("### 9.3", start)]


def test_the_tool_table_lists_every_registered_tool():
    """§9.2 是"模型能做什么"的唯一清单，漏一行就等于漏一条能力。"""
    table = _tech_tool_table()
    missing = [name for name in _registered_tools() if f"`{name}`" not in table]
    assert missing == [], f"§9.2 工具清单少了这些已注册的工具：{missing}"


def test_the_tool_count_in_every_doc_matches_the_registry():
    n = len(_registered_tools())
    assert n == 22, f"注册表应该是 22 个工具（16 + Task 27 的 2 + Task 28 的 1 + 改期 2 + 一周视图 1），实际 {n}"
    for rel in TOOL_COUNT_DOCS:
        found = [int(m) for m in re.findall(r"(?:第|共)\s*(\d+)\s*个", _read(rel))]
        assert found, f"{rel} 里找不到「第 N 个工具」这类句子，用例失去意义"
        assert all(v == n for v in found), f"{rel} 的工具数口径是 {found}，注册表是 {n}"


def test_every_boundary_table_declares_bangumi_live_retrieval():
    """live 检索是对话阶段的新出网面，三份文档必须都写着。"""
    for rel in BOUNDARY_DOCS:
        text = _read(rel)
        assert "api.bgm.tv" in text, f"{rel} 没写对话阶段会请求 api.bgm.tv"
        assert "对话阶段" in text, f"{rel} 没把 Bangumi live 检索标成「对话阶段」"


def test_the_expansion_guide_points_at_the_real_module_and_real_tests():
    """§9.3 是"加工具"的入口，指错路径等于让下一个人白跑一趟。"""
    text = _read(TECH)
    assert "tools/__init__.py" not in text, "§9.3 还写着不存在的 tools/__init__.py"
    assert "test_tool_trigger.py" not in text, "§9.3 的 DoD 还指向不存在的用例文件"
    assert "tools/registry.py" in text, "§9.3 没指向真正的注册模块"


def test_the_retry_budget_matches_the_implementation():
    """安全约束表里的承诺必须与常量一致，否则它是第二张 HANDOFF §2.4。"""
    text = _read(TECH)
    assert "不重试超过 1 次" not in text, "§9.4 还写着「外部 API 不重试超过 1 次」"
    assert "MAX_RETRIES" in text or "MAX_ATTEMPTS" in text, "§9.4 没写重试次数到底在哪个常量里"
