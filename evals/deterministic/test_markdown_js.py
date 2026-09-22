"""前端 Markdown 渲染器：把 ``node --test`` 的测试台挂进 pytest。

前端的用例必须能跟着 ``pytest evals/deterministic`` 一起跑——否则"改了 app.js 顺手
改坏渲染器"这件事没有任何闸门。项目刻意不引 package.json（无构建、无依赖），所以这里
直接调发行版自带的 ``node --test``；机器上没有 node 就跳过，不假装通过。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "yixiang" / "web" / "static" / "markdown.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="这台机器没有 node：跳过前端用例")
def test_markdown_renderer_suite_passes() -> None:
    assert SCRIPT.is_file(), f"前端用例不在：{SCRIPT}"
    done = subprocess.run(
        ["node", "--test", str(SCRIPT)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
