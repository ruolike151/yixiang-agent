"""doctor 六项自检（PART-1 §1 "自检通过"、TECH §1.2）。

口径有意分成两半，用例把这两半都钉住：
  * **没配 key 时不该失败**——检查 1 判"配置能否加载并校验"，检查 5 跳过并告警；
    本地 clone 下来不填 key 也能把 doctor 跑到"0 失败"，否则连 /tools 都试不了。
  * **检查 6 真的会复制**——``templates/`` 的三文件落到 ``data/``，且第二次跑只保留不覆盖。
"""

from __future__ import annotations

from pathlib import Path

from yixiang.config import Settings
from yixiang.ops import doctor

LABELS = (
    "配置加载与校验",
    "data 目录可写",
    "SQLite 与迁移",
    "sqlite-vec 扩展",
    "模型探活",
    "三文件就位",
)


def no_key_settings(tmp_path: Path, repo_root: Path, **overrides) -> Settings:
    """doctor 用例专用的配置：**没有密钥**。

    这里不能复用 conftest 的 ``settings`` 夹具——它带一个假 key，检查 5 会真的去连
    模型（确定性用例一条网络请求都不许发，§13.6）。
    """
    values = {"api_key": "", "data_dir": tmp_path / "data"}
    values.update(overrides)
    return Settings.load(env_file=None, environ={}, project_root=repo_root, **values)


def test_doctor_runs_six_checks_and_warns_but_never_fails_without_api_key(tmp_path, repo_root):
    checks = doctor.run_checks(no_key_settings(tmp_path, repo_root))

    assert [check.label for check in checks] == list(LABELS)
    assert all(check.status != doctor.FAIL for check in checks)
    assert [check.label for check in checks if check.status == doctor.WARN] == [
        "配置加载与校验",
        "模型探活",
    ]
    assert doctor.main(no_key_settings(tmp_path, repo_root)) == 0
    assert "4 项通过，2 项告警" in doctor.render(checks)


def test_doctor_copies_templates_into_data_dir_and_keeps_them(tmp_path, repo_root):
    settings = no_key_settings(tmp_path, repo_root)
    soul_path = settings.data_dir / "soul.md"
    assert not soul_path.exists()  # 起点：data/ 里什么都没有

    first = doctor.run_checks(settings)[-1]

    assert first.status == doctor.OK
    for name in ("soul.md", "user.md", "memory.md"):
        copied = settings.data_dir / name
        assert copied.is_file()
        template = (repo_root / "templates" / name).read_text(encoding="utf-8")
        assert copied.read_text(encoding="utf-8") == template
    assert "新建 soul.md, user.md, memory.md" in first.detail

    second = doctor.run_checks(settings)[-1]

    assert "新建 无" in second.detail  # 第二次跑不覆盖已有的记忆文件
    assert "保留 soul.md, user.md, memory.md" in second.detail


def test_doctor_fails_when_qq_is_enabled_without_allowlist(tmp_path, repo_root):
    """安全默认值（§3.1）：开了 QQ 却没写白名单 → 检查 1 直接失败，不许启动。"""
    settings = no_key_settings(tmp_path, repo_root, qq_enabled=True, qq_allowed="")

    checks = doctor.run_checks(settings)

    assert checks[0].status == doctor.FAIL
    assert "YIXIANG_QQ_ALLOWED" in checks[0].detail
    assert doctor.main(settings) == 1


def test_doctor_check_six_fails_when_a_core_file_is_over_the_limit(tmp_path, repo_root):
    """PART-2 §1：doctor 要证明三文件"上限内"，不是只证明"存在"。"""
    settings = no_key_settings(tmp_path, repo_root)
    doctor.run_checks(settings)  # 先让检查 6 把 templates/ 复制到 data/
    soul = settings.data_dir / "soul.md"
    soul.write_text(soul.read_text(encoding="utf-8") + "x" * 9000, encoding="utf-8")

    check = doctor.run_checks(settings)[-1]

    assert check.status == doctor.FAIL
    assert "soul.md" in check.detail and "超过上限" in check.detail
    assert doctor.main(settings) == 1
