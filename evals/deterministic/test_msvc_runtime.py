"""MSVC 运行库对齐：2026-10-10「聊着聊着整个进程没了」的回归用例。

现场（Windows 的 WER 报告作证）：``winrt``（"正在播放"靠它）在包目录里自带一份 VS2019
时代的 ``MSVCP140.dll``（本机 14.29.30157.0），而 CPython 加载扩展走
``LOAD_WITH_ALTERED_SEARCH_PATH``——**先看 ``.pyd`` 自己所在的目录**，于是它被钉进进程；
之后 fastembed 叫醒 onnxruntime（VS2022 编，要 14.4x），旧运行库喂新二进制 =
``0xC0000005``。这是进程级原生崩溃：``except Exception`` 拦不住，那一轮 trace 一个字都没有，
故障模块正是 winrt 目录下那份 ``MSVCP140.dll``。

所以这里钉四件事：① 包 import 完就已经对齐过（任何入口都躲不掉）；② 重复调用幂等，不会把
已经加载好的那份换掉；③ 进程里真实那份来自 ``System32``，**不是** site-packages 里的副本；
④ 降级口径——找不到 / 加载不了都只留一句话，绝不抛（doctor 与 CLI 不该因此起不来）。

「没做到就会崩」这件事**只能在子进程里问**：真出事时死的整个进程，pytest 自己也一起没了，
进程内的断言根本来不及写。非 Windows 上全部只断言"不做这件事、也不报错"。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from yixiang import MSVC_RUNTIME
from yixiang.runtime import msvc_runtime

REPO_ROOT = Path(__file__).resolve().parents[2]
IS_WINDOWS = sys.platform == "win32"

# 原现场的两种形状：Web 启动即建 MusicService（winrt），用户那句「搜索番剧」才叫醒
# fastembed/onnxruntime。两条都测——CLI 路径本来就不 import winrt，别拿它当证据。
REVERSE_ORDER_SNIPPETS = (
    "import yixiang.media.smtc; import fastembed",
    "import yixiang.web.console; import fastembed",
)


def test_package_import_already_aligned_the_runtime():
    """不变量挂在**包 import 期**：CLI、Web、用例三条入口走的是同一个 ``yixiang/__init__``。"""
    # 换个短名字再断言：两个都写成大写的常量放两侧，ruff 会当成 Yoda 条件（SIM300）
    expected = "ok" if IS_WINDOWS else "not-windows"
    actual = MSVC_RUNTIME

    assert msvc_runtime.unify_msvc_runtime() == MSVC_RUNTIME
    assert actual == expected


def test_unify_is_idempotent():
    """幂等：第二、第三次调用不能把已经加载好的那份换掉。"""
    before = msvc_runtime.loaded_msvcp_path()

    assert msvc_runtime.unify_msvc_runtime() == MSVC_RUNTIME
    assert msvc_runtime.loaded_msvcp_path() == before


@pytest.mark.skipif(not IS_WINDOWS, reason="MSVCP140 是 Windows 上的事")
def test_the_msvcp_in_the_process_is_the_system_one():
    """进进程的那份必须是 ``System32`` 的——winrt 目录里的副本一进来，onnxruntime 就没了。"""
    path = msvc_runtime.loaded_msvcp_path()

    assert path, "对齐之后进程里应该已经有一份 MSVCP140"
    assert Path(path).name.lower() == "msvcp140.dll"
    assert Path(path).parent.name.lower() == "system32", f"抢到的不是系统那份：{path}"
    assert "site-packages" not in path.lower(), f"winrt 自带的那份进来了：{path}"


def test_non_windows_is_a_quiet_no_op(monkeypatch):
    """非 Windows 上只记一句 ``not-windows``：这层是给 Windows 兜底的，不是跨平台依赖。"""
    monkeypatch.setattr(msvc_runtime.sys, "platform", "linux")
    monkeypatch.setattr(msvc_runtime, "_DONE", None)

    assert msvc_runtime.unify_msvc_runtime() == "not-windows"
    assert msvc_runtime.loaded_msvcp_path() is None


@pytest.mark.skipif(not IS_WINDOWS, reason="只有 Windows 才有这条加载路径")
def test_a_copy_that_cannot_be_loaded_reports_instead_of_raising(monkeypatch, tmp_path):
    """候选文件不是合法 DLL 时返回 ``failed:...``——这条路上抛异常等于让 doctor 起不来。"""
    bogus = tmp_path / "msvcp140.dll"
    bogus.write_bytes(b"not a dll")
    monkeypatch.setattr(msvc_runtime, "_DONE", None)
    monkeypatch.setattr(msvc_runtime, "_candidates", lambda: [bogus])

    assert msvc_runtime.unify_msvc_runtime().startswith("failed:")


def test_missing_system_copy_warns_instead_of_going_silent(monkeypatch, tmp_path, capsys):
    """装了 winrt 又找不到系统运行库 = 这一进程有崩溃风险：降级可以，不留消息不行。

    上一轮的教训就在这里——没有日志的悬案比崩溃本身更难查。
    """
    monkeypatch.setattr(msvc_runtime, "_DONE", None)
    monkeypatch.setattr(msvc_runtime, "_candidates", lambda: [tmp_path / "nope.dll"])
    monkeypatch.setattr(msvc_runtime, "_bundled_copy", lambda: tmp_path / "MSVCP140.dll")

    assert msvc_runtime.unify_msvc_runtime() == "no-system-copy"
    warning = capsys.readouterr().err
    assert "MSVCP140" in warning
    assert "可再发行组件包" in warning


@pytest.mark.slow
@pytest.mark.parametrize("snippet", REVERSE_ORDER_SNIPPETS)
def test_a_fresh_interpreter_survives_winrt_before_onnxruntime(snippet):
    """反序 import（winrt 先、onnxruntime 后）在**新解释器**里必须干净退出。

    这是原现场最接近的复现，也是唯一能测的形状：崩溃发生在解释器之外。
    修复前这里是 ``exit=-1073741819``（``0xC0000005``），修复后是 0。
    """
    for name in ("winrt", "onnxruntime", "fastembed"):
        if importlib.util.find_spec(name) is None:
            pytest.skip(f"这台机器没有 {name}，跳过")

    done = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )

    # 退出码用十六进制看：0xC0000005 一眼就是访问冲突，比十进制好认
    assert done.returncode == 0, (
        f"{snippet!r} 在新解释器里以 {done.returncode:#x} 退出"
        f"（0xc0000005 = 原生崩溃）；stderr={done.stderr[-2000:]}"
    )
