"""doctor 七项自检（PART-1 §1 "自检通过"、TECH §1.2）。

口径有意分成两半，用例把这两半都钉住：
  * **没配 key 时不该失败**——检查 1 判"配置能否加载并校验"，检查 5 跳过并告警；
    本地 clone 下来不填 key 也能把 doctor 跑到"0 失败"，否则连 /tools 都试不了。
  * **检查 6 真的会复制**——``templates/`` 的三文件落到 ``data/``，且第二次跑只保留不覆盖。
  * **检查 7 只看副本存不存在**——没配 key 不适用（告警），备份过就转 OK。
"""

from __future__ import annotations

from pathlib import Path

from yixiang import config
from yixiang.config import Settings
from yixiang.ops import doctor

LABELS = (
    "配置加载与校验",
    "data 目录可写",
    "SQLite 与迁移",
    "sqlite-vec 扩展",
    "模型探活",
    "三文件就位",
    "密钥可恢复性",
)


def check_by_label(checks, label: str):
    """按标签取项：第 7 项一加，``[-1]`` 就不再是"三文件就位"了。"""
    return next(check for check in checks if check.label == label)


def no_key_settings(tmp_path: Path, repo_root: Path, **overrides) -> Settings:
    """doctor 用例专用的配置：**没有密钥**。

    这里不能复用 conftest 的 ``settings`` 夹具——它带一个假 key，检查 5 会真的去连
    模型（确定性用例一条网络请求都不许发，§13.6）。
    """
    values = {"api_key": "", "data_dir": tmp_path / "data"}
    values.update(overrides)
    return Settings.load(env_file=None, environ={}, project_root=repo_root, **values)


def test_doctor_runs_seven_checks_and_warns_but_never_fails_without_api_key(tmp_path, repo_root):
    checks = doctor.run_checks(no_key_settings(tmp_path, repo_root))

    assert [check.label for check in checks] == list(LABELS)
    assert all(check.status != doctor.FAIL for check in checks)
    assert [check.label for check in checks if check.status == doctor.WARN] == [
        "配置加载与校验",
        "模型探活",
        "密钥可恢复性",  # 还没备份过 → 告警，不阻塞
    ]
    assert doctor.main(no_key_settings(tmp_path, repo_root)) == 0
    assert "4 项通过，3 项告警" in doctor.render(checks)


def test_doctor_copies_templates_into_data_dir_and_keeps_them(tmp_path, repo_root):
    settings = no_key_settings(tmp_path, repo_root)
    soul_path = settings.data_dir / "soul.md"
    assert not soul_path.exists()  # 起点：data/ 里什么都没有

    first = check_by_label(doctor.run_checks(settings), "三文件就位")

    assert first.status == doctor.OK
    for name in ("soul.md", "user.md", "memory.md"):
        copied = settings.data_dir / name
        assert copied.is_file()
        template = (repo_root / "templates" / name).read_text(encoding="utf-8")
        assert copied.read_text(encoding="utf-8") == template
    assert "新建 soul.md, user.md, memory.md" in first.detail

    second = check_by_label(doctor.run_checks(settings), "三文件就位")

    assert "新建 无" in second.detail  # 第二次跑不覆盖已有的记忆文件
    assert "保留 soul.md, user.md, memory.md" in second.detail


def test_doctor_fails_when_qq_is_enabled_without_allowlist(tmp_path, repo_root):
    """安全默认值（§3.1）：开了 QQ 却没写白名单 → 检查 1 直接失败，不许启动。"""
    settings = no_key_settings(tmp_path, repo_root, qq_enabled=True, qq_allowed="")

    checks = doctor.run_checks(settings)

    assert checks[0].status == doctor.FAIL
    assert "YIXIANG_QQ_ALLOWED" in checks[0].detail
    assert doctor.main(settings) == 1


def test_validate_accepts_the_offline_hash_backend_but_not_a_typo(tmp_path, repo_root):
    """``YIXIANG_EMBED_BACKEND=hash`` 是 CI 与无网演示都在用的离线后端（不下载模型），

    白名单必须认它——否则 ``doctor`` 会对一个官方配置项报错，而 CI 跑的正是这个值。
    打错字的 backend 仍然要被抓出来（白名单的本职）。
    """
    ok = no_key_settings(tmp_path, repo_root, embed_backend="hash")
    assert [e for e in ok.validate() if "EMBED_BACKEND" in e] == []

    typo = no_key_settings(tmp_path, repo_root, embed_backend="fasembed")
    assert [e for e in typo.validate() if "EMBED_BACKEND" in e]


def test_doctor_check_six_fails_when_a_core_file_is_over_the_limit(tmp_path, repo_root):
    """PART-2 §1：doctor 要证明三文件"上限内"，不是只证明"存在"。"""
    settings = no_key_settings(tmp_path, repo_root)
    doctor.run_checks(settings)  # 先让检查 6 把 templates/ 复制到 data/
    soul = settings.data_dir / "soul.md"
    soul.write_text(soul.read_text(encoding="utf-8") + "x" * 9000, encoding="utf-8")

    check = check_by_label(doctor.run_checks(settings), "三文件就位")

    assert check.status == doctor.FAIL
    assert "soul.md" in check.detail and "超过上限" in check.detail
    assert doctor.main(settings) == 1


def test_doctor_check_seven_turns_ok_after_a_secrets_backup(tmp_path, repo_root):
    """第 7 项要能真的翻绿：备份一份 ``.env`` 之后，详细行报出最近那份副本。

    这里**不能**填真 key——检查 5 会拿着它去连模型，确定性用例一条网络请求都不许发。
    所以第 7 项的判据是"副本在不在"，不是"key 配没配"：副本就是可恢复性的全部证据。
    """
    from yixiang.ops import backup

    env_file = tmp_path / ".env"
    env_file.write_text("YIXIANG_API_KEY=sk-test-not-real\n", encoding="utf-8")
    settings = no_key_settings(tmp_path, repo_root)
    backup.backup_secrets(settings.data_dir, env_file)

    check = check_by_label(doctor.run_checks(settings), "密钥可恢复性")

    assert check.status == doctor.OK
    assert ".env." in check.detail


def test_validate_rejects_a_main_model_that_cannot_call_tools(tmp_path, repo_root):
    """主模型必须能调工具：路由到不支持 function calling 的模型 = 整条 loop 退化成聊天。"""
    bad = no_key_settings(tmp_path, repo_root, main_model="deepseek-reasoner")
    problems = [item for item in bad.validate() if "MAIN_MODEL" in item]

    assert problems
    assert "deepseek-reasoner" in problems[0]  # 错误里点名是哪个模型，照着改就行

    good = no_key_settings(tmp_path, repo_root, main_model="deepseek-flash")
    assert [item for item in good.validate() if "MAIN_MODEL" in item] == []


def test_the_capability_guard_bans_only_the_main_role(tmp_path, repo_root, monkeypatch):
    """能力那档是**按角色**的：gate / judge / utility 不调工具，配"不会调工具的模型"也没问题。

    这里用一个假想的名字并临时改 ``NO_TOOL_MODELS``：真实能举的例子（``deepseek-reasoner``）
    同时踩在退役口径上，两条错误混在一起，就分不清是哪一档在拦了。
    """
    monkeypatch.setattr(config, "NO_TOOL_MODELS", ("no-tool-model-x",))
    settings = no_key_settings(
        tmp_path,
        repo_root,
        main_model="deepseek-flash",
        gate_model="no-tool-model-x",
        judge_model="no-tool-model-x",
        utility_model="no-tool-model-x",
    )

    assert [item for item in settings.validate() if "不支持工具调用" in item] == []

    settings.main_model = "no-tool-model-x"
    problems = [item for item in settings.validate() if "不支持工具调用" in item]
    assert problems and "MAIN_MODEL" in problems[0]  # 同一个名字当主模型才被拦


def test_validate_rejects_a_retired_model_in_any_role(tmp_path, repo_root):
    """在售口径是**另一个格子**：下架的名字配到哪个角色都不该放行。

    2026-09-20 实测：官网在售只有 ``deepseek-flash`` / ``deepseek-v4-pro``；``deepseek-chat`` 与
    ``deepseek-reasoner`` 调用仍返回 200，但响应里的 model 被换成 ``deepseek-flash``。
    """
    settings = no_key_settings(
        tmp_path,
        repo_root,
        main_model="deepseek-chat",
        gate_model="deepseek-chat",
        judge_model="deepseek-chat",
        utility_model="deepseek-chat",
    )

    joined = "；".join(settings.validate())

    for role in ("MAIN", "GATE", "JUDGE", "UTILITY"):
        assert f"YIXIANG_{role}_MODEL" in joined, f"{role} 的退役名字没被拦下"
    assert "已下架" in joined


def test_capability_guard_and_retired_guard_are_two_separate_checks(tmp_path, repo_root):
    """两档守门各管一件事，别合并成一条：能力（不会调工具） vs 在售口径（名字已下架）。

    ``deepseek-reasoner`` 两头都占（R1 系不支持 function calling，且已下架），所以两条都要出现；
    只写一条的话，下一个人分不清是哪一类问题，也就不知道该换模型还是该换判断依据。
    """
    settings = no_key_settings(tmp_path, repo_root, main_model="deepseek-reasoner")

    problems = [item for item in settings.validate() if "MAIN_MODEL" in item]

    assert any("不支持工具调用" in item for item in problems)
    assert any("已下架" in item for item in problems)


def test_doctor_fails_when_the_main_model_cannot_call_tools(tmp_path, repo_root):
    """不是"告警"而是"失败"：带着这个配置启动，每一轮都会少一只胳膊。"""
    settings = no_key_settings(tmp_path, repo_root, main_model="deepseek-reasoner")

    checks = doctor.run_checks(settings)

    assert checks[0].status == doctor.FAIL
    assert "MAIN_MODEL" in checks[0].detail
    assert doctor.main(settings) == 1


def test_the_qq_listen_default_does_not_collide_with_the_web_default_port(tmp_path, repo_root):
    """P2 一开网关，两个监听不能撞同一个端口。

    撞了以后报错指向的是"端口被占用"，跟 QQ 一点关系都没有——排查成本远高于现在
    改一个默认值。所以这条钉的是**默认值**，不是文档措辞。
    """
    from yixiang.web.server import DEFAULT_HOST, DEFAULT_PORT

    default = Settings.load(
        env_file=None, environ={}, project_root=repo_root, data_dir=tmp_path / "data"
    )

    assert f"{DEFAULT_HOST}:{DEFAULT_PORT}" != default.qq_listen
    assert default.qq_listen.partition(":")[2] != str(DEFAULT_PORT)


def test_env_example_ships_the_decoupled_qq_port(repo_root):
    """示例文件是别人抄配置的来源，它得跟代码默认值一致，否则抄的人第一脚就踩雷。"""
    text = (repo_root / ".env.example").read_text(encoding="utf-8")

    assert "YIXIANG_QQ_LISTEN=127.0.0.1:8766" in text
