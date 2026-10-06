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


STAGE = STATIC / "lyrics-stage"
STAGE_ORIGINAL = STAGE / "original"
STAGE_FILES = ("app.js", "index.html", "lyric-layout.mjs", "lyric-presentation.mjs", "styles.css")
UPSTREAM_COMMIT = "0f35fbeab379d7ddb85df875d8c9fe38fdc74e7a"


def test_lyrics_stage_assets_are_vendored_verbatim_with_attribution() -> None:
    """歌词舞台：MIT 的 original 渲染器要原样躺在仓库里，来源与许可钉在文件上。

    这里只钉四件事：东西在不在、是不是那一版（三段握手 + 六个模式 + 逐句窗口）、
    那两处文案改没改、版权声明带没带。**"六个模式好不好看"机器判断不了**，
    交给 Step 5 的人眼清单——静态断言冒充不了浏览器。
    """
    for name in STAGE_FILES:
        assert (STAGE_ORIGINAL / name).is_file(), f"舞台缺文件：{name}"

    app = (STAGE_ORIGINAL / "app.js").read_text(encoding="utf-8")
    html = (STAGE_ORIGINAL / "index.html").read_text(encoding="utf-8")
    presentation = (STAGE_ORIGINAL / "lyric-presentation.mjs").read_text(encoding="utf-8")

    # 三段握手：父页发 midori-lyrics-state、舞台自报 midori-lyrics-ready、引擎名不变。
    assert "midori-lyrics-state" in app
    assert "midori-lyrics-ready" in app
    assert "engine: 'original'" in app
    # 六个模式一个不少，顺序照上游 TEMPLATES。
    for mode in ("luminous", "blueprint", "cinema", "pixel", "cloud", "mindscape"):
        assert f"'{mode}'" in app, f"original 少了一个模式：{mode}"
    # 逐句高亮：只渲当前句与上下各两句（index-2..index+2）。
    assert "for (let offset = -2; offset <= 2; offset += 1)" in app
    # 唯一改过的两处文案：上游写死 Spotify 的句子必须已经换掉。
    assert "Spotify" not in presentation
    assert "等待播放" in presentation
    assert "打开任意播放器放音乐" in presentation
    # index.html 是舞台的挂载点：这几个 id 缺一个就白屏。
    for element_id in ("fxCanvas", "lyricStack", "lineCounter", "progressFill"):
        assert f'id="{element_id}"' in html, f"index.html 少了挂载点：{element_id}"

    license_text = (STAGE / "licenses" / "original-MIT.txt").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    assert "Copyright (c) 2026 Tim and contributors" in license_text

    readme = (STAGE_ORIGINAL / "README.md").read_text(encoding="utf-8")
    assert "tosya301/midori-companion-avatar" in readme, "舞台 README 必须写清来源仓库"
    assert UPSTREAM_COMMIT in readme, "舞台 README 必须写清来源 commit（上游会变）"
    assert "AGPL" in readme, "舞台 README 要写清许可分层（original 是 MIT，Folia 是 AGPL）"
