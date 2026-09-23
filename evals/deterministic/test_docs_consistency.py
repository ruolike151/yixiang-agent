"""文档口径一致性：把会随代码漂移的数字钉成断言。

HANDOFF §2.4 那张表（"文档与实现不一致，已全部修掉"）是靠人肉一条条找出来的。
这份用例把其中**会继续漂移**的四类钉住：doctor 项数、门禁项数、前端栏数、
"某样东西还不存在"的陈述句；另外两条盯 README 目录树与 HANDOFF §1.1
（Task 29 点名留给 Task 25 的 T1 遗留口径）。

扫描范围是**当前形态文档**（README / HANDOFF / NUMBERS / architecture / demo 剧本 /
CLI 帮助 / TECH-DESIGN 的事实句），外加几处真代码（`doctor.py` 的检查标签、
`release_gate.py` 的阈值常量、`app.js` 的 `PANELS`）。
`docs/parts/` 与 `docs/PRODUCT.md` 是交付当时的基线快照，记录"当时是什么形态"，
**不回改也不参与断言**——否则每加一个面板都要回头重写历史验收记录。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 会随代码漂移的"当前形态"文档
CURRENT_DOCS = (
    "README.md",
    "docs/HANDOFF.md",
    "docs/NUMBERS.md",
    "docs/architecture.md",
    "scripts/demo-week4.md",
)

# 会提 doctor 项数的文件（architecture.md 不提项数，所以不在这一组）
DOCTOR_DOCS = CURRENT_DOCS + ("yixiang/__main__.py",)

# 过期口径：Task 4 之后 doctor 是七项，Task 28 之后是八项——"六项/七项"都过期了
STALE_DOCTOR = (
    re.compile(r"六项自检"),
    re.compile(r"六项启动自检"),
    re.compile(r"七项自检"),
    re.compile(r"七项启动自检"),
)

# 过期口径：release_gate 一直是**五项**（Task 20「前缀缓存进巡检」本次不做）。
# 这里钉的是"说成六项"的写法；`explain-search` 的"五段"是另一件事，不在模式里。
STALE_GATE = (
    re.compile(r"release_gate`?\s*六项"),
    re.compile(r"门禁的六项"),
    re.compile(r"六项判定"),
    re.compile(r"六项汇总"),
    re.compile(r"六项检查"),
    re.compile(r"六个检查项"),
)

# 过期口径：Task 16 之后前端是 7 栏（第 7 栏 = 链路 trace）
STALE_PANELS = (re.compile(r"六栏"), re.compile(r"六个面板"), re.compile(r"六件测试时"))

# 目录树里的 `scheduler.py` 是设计期的单文件写法，实现是 `scheduler/` 包。
# 负向前瞻排除 `test_scheduler.py`——那是用例文件名，合法。
STALE_SCHEDULER_MODULE = re.compile(r"(?<![A-Za-z0-9_])scheduler\.py")

# TECH-DESIGN 里"仓库还没有代码"的陈述句。
# 注意：§9.2 给 P2 两个**工具**（`bilibili_search` / `pixiv_download`）标的
# "设计位，尚未落盘"是**正确口径**，故意不进这个列表。
STALE_TECH_ABSENT = (
    "还没有代码",
    "里只有设计文档",
    "连骨架也没有",
    "P2 才写",
)

# 任务落地后就不该再出现的"还没有 / 还没接"陈述句
STALE_ABSENT_CLAIMS = {
    "yixiang/__main__.py": (re.compile(r"本阶段未实现"),),
    "yixiang/gateway/__init__.py": (re.compile(r"只有 CLI"),),
    "yixiang/web/console.py": (re.compile(r"P2 才接网关"),),
    "yixiang/web/static/app.js": (re.compile(r"P2 才接网关"),),
}

# README 目录树必须点到的路径——每一个都实测存在于仓库里
README_TREE_NEEDLES = (
    "cli.py",
    "qq.py",
    "sinks.py",
    "runtime.py",
    "evals/",
    "live/",
    "workflows/ci.yml",
    "HANDOFF.md",
    "SECRETS-RECOVERY.md",
    "golden-decisions.md",
    "TODO-AFTER-PART-4.md",
    "parts/",
)


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _assert_no(rel: str, patterns: tuple[re.Pattern[str], ...]) -> None:
    text = _read(rel)
    for pattern in patterns:
        assert not pattern.search(text), f"{rel} 还有过期口径：/{pattern.pattern}/"


def test_doctor_count_is_eight_in_every_file_that_mentions_it() -> None:
    """*任何*文件都不许写旧项数；**提了"自检"的文件**必须写八项。

    `docs/architecture.md` 只提 `doctor` 这个命令名、不提项数，所以它属于前半句、
    不属于后半句——这才是"every file that mentions it"的字面意思。
    """
    mentioned = 0
    for rel in DOCTOR_DOCS:
        text = _read(rel)
        _assert_no(rel, STALE_DOCTOR)
        if "自检" in text:
            mentioned += 1
            assert "八项" in text, f"{rel} 提了自检，却没跟上 doctor 的 8 项"
    assert mentioned >= 3, "至少 README / HANDOFF / CLI 三处要提 doctor 的项数"

    listing = [line for line in _read("README.md").splitlines() if "启动自检" in line]
    assert any("密钥" in line for line in listing), "README 的 doctor 行没列上第 7 项"

    source = _read("yixiang/ops/doctor.py")
    assert "_check_secrets_recovery" in source
    assert "密钥可恢复性" in source


def test_gate_count_is_five_in_every_file_that_mentions_it() -> None:
    for rel in CURRENT_DOCS + ("yixiang/ops/release_gate.py",):
        _assert_no(rel, STALE_GATE)

    for rel in (
        "README.md",
        "docs/NUMBERS.md",
        "docs/architecture.md",
        "scripts/demo-week4.md",
    ):
        assert "五项" in _read(rel), f"{rel} 没跟上门禁的 5 项"

    gate = _read("yixiang/ops/release_gate.py")
    assert gate.count("五项") >= 3, "release_gate.py 自己的口径也不该漂"
    assert "CACHE_HIT_TARGET" not in gate, "Task 20（前缀缓存进巡检）本次不做，门禁仍是五项"


def test_panel_count_is_seven_in_docs_and_in_app_js() -> None:
    for rel in CURRENT_DOCS:
        _assert_no(rel, STALE_PANELS)
        text = _read(rel)
        assert re.search(r"七(栏|个面板)", text), f"{rel} 的栏数还是旧的"
        assert "链路 trace" in text, f"{rel} 没写上第 7 栏"

    source = _read("yixiang/web/static/app.js")
    block = source.split("const PANELS = [", 1)[1].split("\n];", 1)[0]
    ids = re.findall(r'^\s+id: "([a-z]+)"', block, re.MULTILINE)
    assert ids == ["chat", "history", "persona", "config", "prompt", "qq", "traces"]


def test_tech_design_no_longer_claims_the_code_is_missing() -> None:
    tech = _read("docs/TECH-DESIGN.md")
    for stale in STALE_TECH_ABSENT:
        assert stale not in tech, f"TECH-DESIGN 还写着「{stale}」"
    assert not STALE_SCHEDULER_MODULE.search(tech), "目录树还写着 gateway/scheduler.py"


def test_nothing_still_says_a_shipped_thing_is_not_implemented() -> None:
    for rel, patterns in STALE_ABSENT_CLAIMS.items():
        _assert_no(rel, patterns)

    for rel in ("README.md", "docs/architecture.md"):
        assert "只预留接口" not in _read(rel), f"{rel} 还把已落地的东西说成「只预留接口」"


def test_handoff_keeps_its_stale_notes_annotated() -> None:
    handoff = _read("docs/HANDOFF.md")
    section = handoff.split("### 2.4", 1)[1].split("### 2.5", 1)[0]
    if "没有落盘" in section:  # 留档的历史口径，允许保留
        assert "已落盘" in section, "§2.4 的留档条目没说清后来已经落盘"


def test_handoff_section_1_1_matches_the_shipped_corpus() -> None:
    """§1.1 整段是 Task 7 之前的口径，Task 29 把它点名留给了 Task 25。"""
    section = _read("docs/HANDOFF.md").split("### 1.1", 1)[1].split("### 1.2", 1)[0]
    for stale in ("一次都没有", "唯一一处形态完整但没上过真网", "**31 部**"):
        assert stale not in section, f"HANDOFF §1.1 还在写「{stale}」"
    assert "333" in section, "§1.1 没写上真抓之后的语料规模"
    assert "跑过" in section, "§1.1 没说清真实抓取已经跑过"


def test_readme_tree_lists_what_now_exists() -> None:
    tree = _read("README.md").split("## 目录结构", 1)[1].split("```", 2)[1]
    for needle in README_TREE_NEEDLES:
        assert needle in tree, f"README 目录树缺 {needle}"
