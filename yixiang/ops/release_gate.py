"""发布门禁：五项检查的汇总判定（TECH §11.2、PART-4 §4 的冻结契约）。

一句话：**CI 只负责"跑"，判定逻辑只有这一份**。GitHub Actions 的最后一步就是
``python -m yixiang.ops.release_gate``，退出码 0 = 全过、1 = 有硬门禁失败。把判定
写在 workflow 的 YAML 里，等于把考核标准复制成两份，改一处漏一处。

五项与阈值（改动等于改考核标准）：

  1. 确定性用例通过率 = 100%（blocking）——L1+L2 的底线，假 Provider 撑起来的那一层；
  2. judge 均分 ≥ 4.0（blocking）——语气与合理性的回归警报（§13.4 的局限见下）；
  3. 门控漏检率 = 0（blocking）——漏检 = 失忆，是产品级事故；误检容忍 30%（§13.5）；
  4. 检索 top-3 命中率 ≥ 60%（blocking）——``media.jsonl`` 的产品指标；
  5. 单轮成本 ≤ 日预算 × 1.5（**只告警**）——成本超了不该拦住一次正确的合并（§11.2）。

judge 同源局限（§13.4）在这里的体现：第 2 项只当**回归警报**，不当质量结论。它的
分数由 ``evals/judge/cases.yaml`` 的 rubric 算出；能客观判断的部分（工具调没调、
参数对不对、有没有编造）早就下沉成第 1 项的确定性断言了。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yixiang.config import Settings
from yixiang.ops.usage import period_lines, summarize
from yixiang.rag.evaluate import HIT_TARGET
from yixiang.runtime.models import SystemClock

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 冻结阈值（PART-4 §4 的表格，别在别处再抄一遍）
DETERMINISTIC_TARGET = 1.0
JUDGE_TARGET = 4.0
GATE_MISS_TARGET = 0.0
RETRIEVAL_TARGET = HIT_TARGET
COST_BUDGET_FACTOR = 1.5

_PASSED_RE = re.compile(r"(\d+) passed")
_FAILED_RE = re.compile(r"(\d+) (failed|error|errors)")


@dataclass(slots=True)
class Check:
    """一项检查：``value`` 对 ``threshold``（口径见各 ``_*_check`` 的 docstring）。"""

    name: str
    value: float
    threshold: float
    passed: bool
    blocking: bool
    detail: str = ""
    unit: str = ""

    def render(self) -> str:
        mark = "✓" if self.passed else ("✗" if self.blocking else "⚠")
        kind = "硬门禁" if self.blocking else "只告警"
        shown = _format_value(self.value, self.unit)
        target = _format_value(self.threshold, self.unit)
        line = f"  {mark} {self.name:<22} {shown:>10} / 阈值 {target:<10} [{kind}]"
        return f"{line}\n      {self.detail}" if self.detail else line


@dataclass(slots=True)
class GateResult:
    """一次门禁的结果：``passed`` 只看 blocking 项（告警不影响退出码）。"""

    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks if check.blocking)

    @property
    def exit_code(self) -> int:
        return 0 if self.passed else 1

    def render(self) -> str:
        blocking = [check for check in self.checks if check.blocking]
        warnings = [check for check in self.checks if not check.blocking]
        passed = sum(1 for check in blocking if check.passed)
        lines = [f"发布门禁：硬门禁 {passed}/{len(blocking)} 通过"]
        lines.extend(check.render() for check in blocking)
        if warnings:
            lines.append("告警项（不阻止合并）：")
            lines.extend(check.render() for check in warnings)
        lines.append("结论：" + ("全过，可以合并" if self.passed else "有硬门禁失败，不要合并"))
        return "\n".join(lines)


def _format_value(value: float, unit: str) -> str:
    if unit == "%":
        return f"{value * 100:.1f}%"
    if unit == "¥":
        return f"¥{value:.4f}"
    return f"{value:.2f}"


# --------------------------------------------------------------- 五项检查
def pytest_command(target: Path, *extra: str) -> list[str]:
    """门禁跑 pytest 的统一命令（用例也复用它，避免"两处各写一套参数"）。

    ``-o addopts=`` 不是装饰：``pyproject.toml`` 里有 ``addopts = "-q"``，命令里再给一个
    ``-q`` 就是**双 ``-q``**——pytest 在双 ``-q`` 下会连最后那行 ``N passed`` 一起省掉，
    于是门禁拿到的是"0 passed / 0 failed"这种假数字。踩过一次，回归用例在
    ``evals/deterministic/test_release_gate.py``。
    """
    return [
        sys.executable,
        "-m",
        "pytest",
        str(target),
        "-m",
        "not live",
        "-o",
        "addopts=",
        "-q",
        "--tb=line",
        "-p",
        "no:cacheprovider",
        *extra,
    ]


def _deterministic_check(root: Path, *, timeout: float = 300.0) -> Check:
    """第 1 项：跑一遍离线确定性用例，通过率必须 100%（L1+L2，§13.1）。

    这一步自己起 pytest 子进程而不是"相信上一步跑过"：CI 里顺序由 workflow 保证，
    本地手工跑时门禁得能独立站住。live 用例不进这里（外部 API 抖动不该阻塞合并）。
    """
    command = pytest_command(root / "evals" / "deterministic")
    try:
        proc = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(
            name="确定性用例通过率",
            value=0.0,
            threshold=DETERMINISTIC_TARGET,
            passed=False,
            blocking=True,
            unit="%",
            detail=f"跑不起来：{exc}",
        )
    output = (proc.stdout or "") + (proc.stderr or "")
    passed = _count(_PASSED_RE, output)
    failed = _count(_FAILED_RE, output)
    total = passed + failed
    rate = passed / total if total else 0.0
    detail = f"{passed} passed / {failed} failed（pytest 退出码 {proc.returncode}）"
    if total == 0:
        detail += "；输出里没有可解析的汇总行——先去查 pytest 参数是不是被叠加成双 -q"
    return Check(
        name="确定性用例通过率",
        value=rate,
        threshold=DETERMINISTIC_TARGET,
        passed=proc.returncode == 0 and total > 0,
        blocking=True,
        unit="%",
        detail=detail,
    )


def _count(pattern: re.Pattern[str], text: str) -> int:
    return sum(int(match.group(1)) for match in pattern.finditer(text))


def _judge_check(root: Path, *, cases: Path | None = None) -> Check:
    """第 2 项：跑 judge 的 10 条 rubric，均分 ≥ 4.0（离线基线自检，见 run_judge）。

    没有 API key 时跑的是**离线基线**（cases.yaml 里录好候选回复 + 确定性 rubric），
    它检的是"题面、解析器、留痕管线"没坏；有 key 时 nightly 用 ``--live`` 走真模型。
    两种模式共用同一个报告结构，所以门禁不需要分叉。
    """
    module = _load_judge(root)
    report = module.run_cases(cases or module.DEFAULT_CASES, mode="offline")
    return Check(
        name="judge 均分",
        value=report.mean,
        threshold=JUDGE_TARGET,
        passed=report.mean >= JUDGE_TARGET and report.total > 0,
        blocking=True,
        detail=report.summary(),
    )


def _gate_check(root: Path) -> Check:
    """第 3 项：门控标注集的**漏检率必须为 0**，误检容忍 ≤30%（§13.5）。

    口径与 ``evals/deterministic/test_gate.py`` 完全一致（同一份 golden、同一个
    离线代理模型 = 规则层 + 自我指涉词下界），因为这里报的 ``value`` 就是那个
    被 print 出来的漏检率；两边算式漂移会让"用例绿了但门禁红了"这种怪事出现。
    """
    module = _load_test_gate(root)
    metrics = module._metrics(module._load_golden())  # noqa: SLF001 - 与用例共用同一口径
    miss = float(metrics["miss_rate"])
    return Check(
        name="门控漏检率",
        value=miss,
        threshold=GATE_MISS_TARGET,
        passed=miss <= GATE_MISS_TARGET,
        blocking=True,
        unit="%",
        detail=(
            f"漏检 {miss * 100:.1f}%（硬门禁 0）· 误检 {metrics['false_alarm_rate'] * 100:.1f}%"
            f"（容忍 ≤30%，实测口径 {metrics['total']:.0f} 条标注）"
        ),
    )


def _retrieval_check(settings: Settings) -> Check:
    """第 4 项：``media.jsonl`` 的 top-3 命中率 ≥60%（L3 检索回归，§8.6）。

    语料缺席就先按离线 fixture 入库（``rag eval --source local --file`` 的同一路径），
    这样干净环境里第一次跑门禁也能出真实数字，而不是"没有语料 → 假装通过"。
    """
    from yixiang.ops import rag_cmd
    from yixiang.rag import evaluate, ingest

    clock = SystemClock()
    embedder = rag_cmd._embedder(settings)  # noqa: SLF001 - 与 CLI 共用装配，避免两套
    golden = rag_cmd.GOLDEN_DIR / "media.jsonl"
    cases = evaluate.load_golden(golden)
    if not cases:
        return Check(
            name="检索 top-3 命中率",
            value=0.0,
            threshold=RETRIEVAL_TARGET,
            passed=False,
            blocking=True,
            unit="%",
            detail=f"golden 集缺失：{golden}（只增不改，见 PART-4 §4）",
        )
    with rag_cmd._session(settings, embedder=embedder, clock=clock) as conn:  # noqa: SLF001
        if evaluate.corpus_size(conn) == 0:
            ingest.ingest_items(
                conn,
                ingest.load_local(rag_cmd.INGEST_FIXTURE, source="local"),
                source="local",
                embedder=embedder,
                clock=clock,
            )
        try:
            report = evaluate.evaluate(
                cases,
                top_k=3,
                corpus=evaluate.corpus_size(conn),
                source=golden.name,
            )
        except Exception as exc:  # 嵌入维度换了 / 索引坏了：如实报，不静默
            return Check(
                name="检索 top-3 命中率",
                value=0.0,
                threshold=RETRIEVAL_TARGET,
                passed=False,
                blocking=True,
                unit="%",
                detail=f"评测跑不起来（{type(exc).__name__}: {exc}）；试试 rag reindex",
            )
        detail = (
            f"{report.hits}/{report.total} 命中 · MRR {report.mrr:.3f} · "
            f"语料 {report.corpus} 部 · 嵌入 {report.embed} · 口味关（T3 口径）"
        )
    return Check(
        name="检索 top-3 命中率",
        value=report.hit_rate,
        threshold=RETRIEVAL_TARGET,
        passed=report.passed(RETRIEVAL_TARGET),
        blocking=True,
        unit="%",
        detail=detail,
    )


def _cost_check(settings: Settings) -> Check:
    """第 5 项：今天最贵的一轮 ≤ 日预算 ×1.5 —— **只告警**（§11.2、§15.2）。

    成本超预算往往是"用户今天聊得多"，不是代码退步；用它拦合并会把门禁变成噪音。
    没有今天的用量时 value = 0（干净环境里门禁不该因为"还没聊过天"报警）。
    """
    today = SystemClock().now().astimezone().date()
    lines = period_lines(settings.usage_path, period="day", ref=today)
    per_turn: dict[str, float] = {}
    for line in lines:
        per_turn[line.turn_id or "(无 turn_id)"] = (
            per_turn.get(line.turn_id or "(无 turn_id)", 0.0) + line.cost_cny
        )
    worst = max(per_turn.values(), default=0.0)
    threshold = settings.budget_cny_per_day * COST_BUDGET_FACTOR
    summary = summarize(settings.usage_path, period="day", ref=today)
    budget = settings.budget_cny_per_day
    return Check(
        name="单轮成本上限",
        value=worst,
        threshold=threshold,
        passed=worst <= threshold,
        blocking=False,
        unit="¥",
        detail=(
            f"今天 {summary['total']['turns']} 轮 · 合计 ¥{summary['total']['cost_cny']:.4f}"
            f"（预算 ¥{budget:.2f}/天）· 最贵一轮上界 ¥{worst:.4f}"
        ),
    )


# --------------------------------------------------------------- 汇总判定
def default_checkers(
    settings: Settings, root: Path
) -> list[Callable[[], Check]]:
    """默认的五个检查项（顺序 = 打印顺序 = 冻结表里的顺序）。"""
    return [
        lambda: _deterministic_check(root),
        lambda: _judge_check(root),
        lambda: _gate_check(root),
        lambda: _retrieval_check(settings),
        lambda: _cost_check(settings),
    ]


def run_gate(
    settings: Settings | None = None,
    *,
    root: Path | str = PROJECT_ROOT,
    checkers: Sequence[Callable[[], Check]] | None = None,
) -> GateResult:
    """跑完五项（或注入的检查项）并给出退出码语义的结论。

    ``checkers`` 是给用例留的注入口：门禁本身的用例不该真的再跑一遍全量 pytest。
    """
    root = Path(root)
    settings = settings or Settings.load(env_file=None, project_root=root)
    checks = [checker() for checker in (checkers or default_checkers(settings, root))]
    return GateResult(checks=checks)


def _load_judge(root: Path) -> Any:
    """``evals/judge/run_judge.py`` 按路径加载（``evals`` 不是包，也不该为它造包）。"""
    return _load_module(root / "evals" / "judge" / "run_judge.py", "yixiang_judge_runner")


def _load_test_gate(root: Path) -> Any:
    """``test_gate.py`` 里的 ``_metrics`` / ``_load_golden`` 是纯函数，直接复用。"""
    path = root / "evals" / "deterministic" / "test_gate.py"
    if str(path.parent) not in sys.path:
        # test_gate 顶部 ``from fake_provider import …`` 依赖同目录在 sys.path 上
        sys.path.insert(0, str(path.parent))
    return _load_module(path, "yixiang_test_gate")


def _load_module(path: Path, name: str) -> Any:
    import importlib.util

    if not path.is_file():
        raise FileNotFoundError(f"缺少文件：{path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 只有路径诡异时发生
        raise ImportError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    # 必须先登记进 sys.modules 再执行：`@dataclass(slots=True)` 在装饰期会回头查
    # `sys.modules[cls.__module__]`，没登记就 AttributeError（这个坑在 pytest 里
    # 恰好被别的 import 掩盖，独立跑 `python -m yixiang.ops.release_gate` 才露出来）
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m yixiang.ops.release_gate",
        description="发布门禁：五项检查的汇总判定（退出码 0 = 全过，1 = 有硬门禁失败）",
    )
    parser.add_argument("--env-file", default=".env", help="配置文件路径（默认 .env）")
    parser.add_argument(
        "--root", default=str(PROJECT_ROOT), help="仓库根（默认按本文件位置推断）"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    settings = Settings.load(env_file=args.env_file, project_root=root)
    result = run_gate(settings, root=root)
    print(result.render())
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "COST_BUDGET_FACTOR",
    "DETERMINISTIC_TARGET",
    "GATE_MISS_TARGET",
    "JUDGE_TARGET",
    "RETRIEVAL_TARGET",
    "Check",
    "GateResult",
    "default_checkers",
    "main",
    "pytest_command",
    "run_gate",
]
