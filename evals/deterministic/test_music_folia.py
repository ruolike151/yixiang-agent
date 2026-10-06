"""Folia 歌词舞台：105 个 AGPL 产物原样 vendor + 宿主 engine 分叉 + 事件桥。

Task 9 的 original 是 MIT、逐句；本任务把参考项目那套 Folia（12 模式、卡拉 OK 逐字）
原样搬进来。这份用例只钉四类东西：

1. `lyrics-stage/folia/` 子树与上游 `docs/FOLIA_SOURCE_MANIFEST.json` **逐文件**对得上
   （105 条：路径 + 字节数 + SHA256；合计 12,917,606 B）；
2. 五个散文件（入口 html / 舞台壳 / 运行时配置 / 两个图标）逐个钉字节数 + 哈希；
3. AGPL 许可原文 + 上游 `THIRD_PARTY.md` + 我们自己写的 `folia/README.md`
   （来源坐标 / AGPL 范围 / "对应源码"边界 / 12 个模式名）；
4. 宿主真的分叉了（`app.js` 里两条协议 + service worker 复位 + `frameUrlFor`；
   `music.mjs` 里 18 个模式的注册），且 Task 9 的 `original/README.md` 已指向新引擎。

`tempera` 是 opt-in：它必须在 `LYRIC_MODES` 里、但**不在**自动轮换池里——
那半边由 `music.test.mjs` 的 node 用例断言（这里只钉"注册了 12 个名字"）。

纯读文件，零网络、零模型；105 个文件的哈希约 0.3 s（12 MB）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "yixiang" / "web" / "static"
STAGE = STATIC / "lyrics-stage"
FOLIA = STAGE / "folia"
MANIFEST = ROOT / "docs" / "FOLIA_SOURCE_MANIFEST.json"

UPSTREAM_COMMIT = "0f35fbeab379d7ddb85df875d8c9fe38fdc74e7a"
FOLIA_BASE_COMMIT = "446e9038b8e43f44d4ed0628ce5b8fc8ad22538c"
FOLIA_BACKPORT_COMMIT = "04d222779b2bd63fd04436cb7477c7de5dd5f008"

DEPLOYED_FILES = 105
TOTAL_BYTES = 12_917_606
FOLIA_MODES = (
    "classic", "cadenza", "partita", "tilt", "fume", "monet",
    "claddagh", "pendolo", "diorama", "sonnet", "cappella", "tempera",
)

# 五个散文件：字节数 + SHA256（`icon.svg.bak` 的哈希由清单那条用例覆盖，
# 这里只钉"它在、大小对"，免得把一份备份图标抄成两份不同的核对表）。
LOOSE_FILES: dict[str, tuple[int, str | None]] = {
    "index.html": (1_801, "0f3c96c24b4545520e1cf02a44a6819ae34fc31de0107141271c3beba127580f"),
    "stage-client.html": (43_425, "3996eace306a6c12c2e5ce3b0ae3d59d5a7391409ad7938978925b78a0b28f3f"),
    "runtime-config.js": (182, "2adb4a514fb1037e4b87aca3755039549fbee2396955faae8871b93ed3f5bff5"),
    "icon.svg": (1_473, "0045ede40bee2e9ac4f40a3a803b16febbf75ab9beca6c438bfbd1e6cd5529a8"),
    "icon.svg.bak": (8_673, None),
}

AGPL_BYTES = 34_523
AGPL_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
THIRD_PARTY_BYTES = 5_282
THIRD_PARTY_SHA256 = "62a5b6995b3cf37b57bff6848f2f32b1955c46b8855c18f9d730f229234e2158"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest() -> dict:
    """上游那份凭据清单。**键名以真实文件为准**：第一次跑之前先 `Get-Content` 看一眼顶层
    键（本计划实测是 `counts` / `deployed_artifacts` / `upstream` / `licenses` / `modes`）；
    真文件用了别的名字就改**这份用例的取键路径**，别去改清单。"""
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _manifest_entries() -> list[dict]:
    entries = _manifest()["deployed_artifacts"]
    assert isinstance(entries, list) and entries, "凭据清单里没有 deployed_artifacts"
    return entries


def _local_path(rel: str) -> Path:
    """清单里的路径是**上游仓库相对**的（`lyrics-stage/folia/...`）；
    本地把它们整棵放在 `yixiang/web/static/` 下，前缀一一对应。"""
    marker = "lyrics-stage/"
    tail = rel[rel.index(marker):] if marker in rel else rel
    return STATIC / tail


def test_folia_subtree_matches_the_source_manifest_file_by_file() -> None:
    """105 个产物：**逐文件**核字节数与 SHA256，再核条目数与总字节。

    这是本任务唯一"真的证明搬对了版本"的用例——哈希对不上就是抓错了，
    别去改清单（见任务的「卡住怎么办」）。
    """
    entries = _manifest_entries()
    assert len(entries) == DEPLOYED_FILES, f"清单条目数 {len(entries)} ≠ {DEPLOYED_FILES}"

    total = 0
    for item in entries:
        path = _local_path(str(item["path"]))
        assert path.is_file(), f"产物不在：{item['path']}"
        size = path.stat().st_size
        assert size == item["bytes"], f"{item['path']} 字节数 {size} ≠ 清单 {item['bytes']}"
        assert _sha256(path) == item["sha256"], f"{item['path']} 的 SHA256 与清单对不上"
        total += size
    assert total == TOTAL_BYTES, f"产物合计 {total} B ≠ {TOTAL_BYTES} B"


def test_folia_loose_entrypoints_are_pinned() -> None:
    for name, (size, digest) in LOOSE_FILES.items():
        path = FOLIA / name
        assert path.is_file(), f"folia 少了散文件：{name}"
        assert path.stat().st_size == size, f"{name} 字节数不对"
        if digest is not None:
            assert _sha256(path) == digest, f"{name} 的 SHA256 与核对表对不上"


def test_folia_agpl_license_and_third_party_notice_are_vendored() -> None:
    license_text = (STAGE / "licenses" / "folia-AGPL-3.0.txt").read_text(encoding="utf-8")
    assert (STAGE / "licenses" / "folia-AGPL-3.0.txt").stat().st_size == AGPL_BYTES
    assert _sha256(STAGE / "licenses" / "folia-AGPL-3.0.txt") == AGPL_SHA256
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in license_text
    assert "Version 3" in license_text

    third_party = (STAGE / "THIRD_PARTY.md").read_text(encoding="utf-8")
    assert (STAGE / "THIRD_PARTY.md").stat().st_size == THIRD_PARTY_BYTES
    assert _sha256(STAGE / "THIRD_PARTY.md") == THIRD_PARTY_SHA256
    assert "AGPL" in third_party
    # 上游原话：代码许可**不**授予美术 / 音乐 / 歌词 / 封面权利。
    assert "美术" in third_party or "artwork" in third_party.lower()


def test_folia_readme_records_provenance_and_agpl_scope() -> None:
    readme = (FOLIA / "README.md").read_text(encoding="utf-8")
    assert "tosya301/midori-companion-avatar" in readme, "要写清来源仓库"
    assert UPSTREAM_COMMIT in readme, "要写清来源 commit（上游会变）"
    assert "AGPL" in readme, "要写清这一棵子树是 AGPL-3.0"
    assert "chthollyphile/folia-major" in readme, "要写清上游引擎"
    assert FOLIA_BASE_COMMIT in readme and FOLIA_BACKPORT_COMMIT in readme
    for mode in FOLIA_MODES:
        assert f"`{mode}`" in readme, f"12 个模式要列全，少了 {mode}"
    # 「对应源码」的边界必须写实（取舍 ④）：凭据清单 + 不自建的理由。
    assert "FOLIA_SOURCE_MANIFEST" in readme
    assert "Node" in readme and "24" in readme, "要写清为什么不自己构建（上游要 Node ≥24）"
    assert "AGPL" in readme and "MIT" in readme, "要把两种许可的边界分开写"


def test_host_speaks_both_stage_protocols_and_resets_the_folia_worker() -> None:
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "midori-lyrics-state" in app, "original 那条协议不能丢"
    assert "midori-lyrics-events" in app, "folia 那条协议要新加"
    assert "midori-lyrics-ready" in app
    assert 'engine: "folia"' in app or "engine: 'folia'" in app
    assert "foliaServiceWorkerReset" in app, "切引擎要先复位 service worker"
    assert "about:blank" in app, "切引擎先清 src（上游 spotify-lyrics-stage.js:375-379）"
    assert "lyricStage=1" in app and "visualizer=" in app, "folia 的入口参数"
    assert "/lyrics-stage/original/index.html" in app, "original 的入口不能改"


def test_music_module_registers_eighteen_modes() -> None:
    music = (STATIC / "music.mjs").read_text(encoding="utf-8")
    for name in ("FOLIA_MODES", "LYRIC_MODES", "AUTOMATIC_LYRIC_MODES", "MODES_BY_ENGINE",
                 "formatLrcTimestamp", "lyricLinesToLrc", "stateToFoliaEvents"):
        assert name in music, f"music.mjs 少了导出：{name}"
    for mode in FOLIA_MODES:
        assert f'"{mode}"' in music or f"'{mode}'" in music, f"少了 Folia 模式：{mode}"


def test_original_readme_points_to_the_folia_sibling_engine() -> None:
    """Task 9 那份 README 里「为什么只有 original，没有 Folia」已经过期：
    本任务之后 Folia **就在隔壁**。改文案，但 `AGPL` 那个词必须留着——
    Task 9 的 `test_lyrics_stage_assets_are_vendored_verbatim_with_attribution` 断言它。"""
    readme = (STAGE / "original" / "README.md").read_text(encoding="utf-8")
    assert "AGPL" in readme
    assert "folia/" in readme, "要指向隔壁的 folia/ 子树"
    assert "Task 10" in readme or "task-10" in readme


def test_folia_manifest_records_counts_and_the_upstream_folia_commits() -> None:
    data = _manifest()
    counts = data["counts"]
    assert counts["deployed_files"] == DEPLOYED_FILES
    text = MANIFEST.read_text(encoding="utf-8")
    assert "chthollyphile/folia-major" in text
    assert FOLIA_BASE_COMMIT in text and FOLIA_BACKPORT_COMMIT in text
