"""前端音乐面板：把 ``node --test`` 的测试台挂进 pytest。

与 ``test_markdown_js.py`` 同一条路：项目刻意不引 package.json（无构建、无依赖），
所以这里直接调发行版自带的 ``node --test``；机器上没有 node 就跳过，不假装通过。
Task 9 还会往这份文件里追加歌词舞台那份资源的静态断言。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "yixiang" / "web" / "static"
MUSIC_SCRIPT = STATIC / "music.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="这台机器没有 node：跳过前端用例")
def test_music_module_suite_passes() -> None:
    assert MUSIC_SCRIPT.is_file(), f"前端用例不在：{MUSIC_SCRIPT}"
    done = subprocess.run(
        ["node", "--test", str(MUSIC_SCRIPT)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
