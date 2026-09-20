"""发布门禁自身的用例（PART-4 §7、§11.2）。

门禁是"唯一一份判定逻辑"，所以它自己是**最不该无条件相信**的那块代码：它静默地报
"通过"，比任何功能 bug 都贵。这里钉住三件事：

  ① 判定项的退出码语义——**告警不影响合并，硬门禁失败必须拦**（§4 的冻结表）；
  ② 门禁跑 pytest 的方式能拿到真实计数（双 ``-q`` 会吃掉汇总行，是真踩过的坑）；
  ③ 门禁按路径加载 judge / 门控用例的方式真的能用（`@dataclass(slots=True)` 要求
     模块先登记进 ``sys.modules``，同样是真踩过的坑）。

不在这里跑整套门禁：那会在用例里再起一次全量 pytest，把 5 秒变成 10 秒，还让失败
信息套两层。整套门禁由 CI 的最后一步负责（``python -m yixiang.ops.release_gate``）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from yixiang.ops.release_gate import (
    _PASSED_RE,
    JUDGE_TARGET,
    Check,
    GateResult,
    _count,
    _gate_check,
    _judge_check,
    _load_judge,
    pytest_command,
)


def _check(name: str, *, passed: bool, blocking: bool) -> Check:
    return Check(
        name=name,
        value=1.0 if passed else 0.0,
        threshold=1.0,
        passed=passed,
        blocking=blocking,
    )


def test_warning_never_blocks_but_hard_gate_does() -> None:
    """成本项只告警：它红了照样 exit 0；硬门禁红了必须 exit 1（§11.2）。"""
    warned = GateResult(
        checks=[
            _check("确定性用例通过率", passed=True, blocking=True),
            _check("单轮成本上限", passed=False, blocking=False),
        ]
    )
    assert warned.passed is True
    assert warned.exit_code == 0
    assert "不阻止合并" in warned.render()

    blocked = GateResult(
        checks=[
            _check("确定性用例通过率", passed=True, blocking=True),
            _check("judge 均分", passed=False, blocking=True),
        ]
    )
    assert blocked.passed is False
    assert blocked.exit_code == 1
    assert "不要合并" in blocked.render()


def test_pytest_summary_is_parseable_for_the_gate(tmp_path: Path) -> None:
    """门禁必须能从输出里读到 ``N passed``——双 ``-q`` 会把它整行吞掉。

    口径用真实命令（``pytest_command``）跑一个真 pytest：一条通过的用例，汇总行里
    必须有 "1 passed"。这样"门禁报 0 passed 却退出码 0"这种假通过不会再回来。
    """
    (tmp_path / "test_ok.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )
    proc = subprocess.run(
        pytest_command(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, output
    assert _count(_PASSED_RE, output) == 1, output


def test_judge_module_loads_and_scores(repo_root: Path) -> None:
    """按路径加载 ``run_judge.py`` 必须成功：模块没登记进 ``sys.modules`` 时，
    ``@dataclass(slots=True)`` 会在装饰期就抛 AttributeError（独立跑门禁才会暴露）。"""
    module = _load_judge(repo_root)
    assert callable(module.run_cases)

    check = _judge_check(repo_root)
    assert check.passed is True
    assert check.value >= JUDGE_TARGET
    assert check.blocking is True


def test_gate_check_shares_the_case_metric(repo_root: Path) -> None:
    """门控项与用例共用同一份口径：漏检率就是别名集上算出来的那个数。"""
    check = _gate_check(repo_root)
    assert check.passed is True
    assert check.value == 0.0
    assert "漏检 0.0%" in check.detail
