"""judge 评测：10 条 rubric 的打分、结构化解析与失败留痕（TECH §13.4、PART-4 §3/§7）。

两种模式共用同一份题面（``cases.yaml``）：

  * ``offline``（默认）——用题面里**录好的候选回复**跑确定性 rubric（``must_have`` /
    ``must_not_have`` / 长度上限）。它证明的是"题面、解析器、留痕管线没坏"，而不是
    "回复写得好"；CI 与干净环境跑的都是这一条，零成本、可重复。
  * ``live``——主模型真生成回复，judge 模型按 rubric 打分并回 ``{score, reasons[]}``。
    它不进 PR 门禁（外部 API 抖动不该阻塞开发，§17.2-2），只跑 nightly 与发版前。

**同源局限是主动交底的**（§13.4）：judge 与 main 是同一族模型，自评有偏好偏差。
所以三条缓解同时存在：① 这个均分只当**回归警报**，不当质量结论；② 能客观判断的
部分（工具调没调、参数对不对、有没有编造）早就下沉成确定性断言；③ 换 judge 模型
时要重跑全部历史分数校准，否则新旧分数不可比。

解析失败**不静默**：该条计 0、``reasons`` 写明原因、模型原始输出留在报告里。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from yixiang.runtime.models import Clock, ProviderRequest, SystemClock, to_local_iso, user_message

try:  # pyyaml 是声明依赖（pyproject）；缺了就是环境坏了，报清楚比崩栈强
    import yaml
except ModuleNotFoundError:  # pragma: no cover - 正常安装下不会走到
    yaml = None  # type: ignore[assignment]

JUDGE_DIR = Path(__file__).resolve().parent
DEFAULT_CASES = JUDGE_DIR / "cases.yaml"
DEFAULT_REPORT_DIR = JUDGE_DIR / "reports"

# 通过线：均值 ≥4.0（PART-4 §4 的冻结阈值，与 release_gate 的 JUDGE_TARGET 同值）
PASS_MEAN = 4.0
SCORE_MIN = 1
SCORE_MAX = 5

# 离线口径的两个上限：缺内容最多扣 2、踩红线最多扣 2，长度单独扣 1（见 score_offline）
MISSING_PENALTY_CAP = 2
FORBIDDEN_PENALTY_CAP = 2
LENGTH_PENALTY = 1

_LIVE_MAX_TOKENS = 700
_GENERATE_MAX_TOKENS = 700
_GENERATE_TEMPERATURE = 0.3

GENERATE_SYSTEM = (
    "你是 yixiang（以湘），一个本地优先的个人助手。按用户这句话给出回复：中文、"
    "直接、长度与场景匹配，不要解释你正在做什么，也不要提及测试或评测。"
)

JUDGE_SYSTEM = (
    "你在给一份个人助手的回复打分。**唯一依据是下面给出的 rubric**，不要参考你"
    "自己的偏好。只输出一个 JSON 对象，不要输出任何别的文字：\n"
    '{"score": 1 到 5 的整数, "reasons": ["一句话理由", "…"]}\n'
    "5 = 完全满足 rubric；4 = 差一项或风格略偏；3 = 差两项；2 = 明显不合格；"
    "1 = 违反硬性要求或跑题。"
)


@dataclass(slots=True)
class JudgeCase:
    """一条 rubric 题面：``reply`` 只在离线模式用，``must_*`` 是客观可判的部分。"""

    id: str
    category: str
    user: str
    reply: str
    rubric: list[str] = field(default_factory=list)
    must_have: list[str] = field(default_factory=list)
    must_not_have: list[str] = field(default_factory=list)
    max_chars: int | None = None
    note: str = ""


@dataclass(slots=True)
class JudgeResult:
    """一条的判分结果；``score == 0`` 表示解析/调用失败（计 0 并留痕）。"""

    id: str
    category: str
    score: int
    reasons: list[str] = field(default_factory=list)
    raw: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "category": self.category,
            "score": self.score,
            "reasons": list(self.reasons),
        }
        if self.raw:
            payload["raw"] = self.raw
        return payload


@dataclass(slots=True)
class JudgeReport:
    """一次 judge 的结果：``mean`` 就是门禁看的那一个数（≥4.0）。"""

    mode: str
    results: list[JudgeResult] = field(default_factory=list)
    cases_path: str = ""
    generated_at: str = ""

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def mean(self) -> float:
        if not self.results:
            return 0.0
        return sum(result.score for result in self.results) / len(self.results)

    @property
    def failed(self) -> list[JudgeResult]:
        return [result for result in self.results if result.score <= 0]

    @property
    def passed(self) -> bool:
        return self.total > 0 and self.mean >= PASS_MEAN and not self.failed

    def summary(self) -> str:
        worst = sorted(self.results, key=lambda item: item.score)[:2]
        low = "；".join(f"{item.id}={item.score}" for item in worst if item.score < SCORE_MAX)
        line = f"{self.total} 条 · 均分 {self.mean:.2f} / 通过线 {PASS_MEAN:.1f}（{self.mode}）"
        if low:
            line += f" · 最低：{low}"
        if self.failed:
            line += f" · 解析失败 {len(self.failed)} 条"
        return line

    def render(self) -> str:
        lines = [self.summary()]
        for result in self.results:
            reason = "；".join(result.reasons[:2]) if result.reasons else "—"
            lines.append(f"  · {result.id} [{result.category}] {result.score}/5 — {reason}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "cases": self.cases_path,
            "generated_at": self.generated_at,
            "total": self.total,
            "mean": round(self.mean, 4),
            "passed": self.passed,
            "results": [result.as_dict() for result in self.results],
        }


# --------------------------------------------------------------------- 题面
def load_cases(path: Path | str = DEFAULT_CASES) -> list[JudgeCase]:
    """读 ``cases.yaml``；题面坏了要在门禁里响，不能悄悄变成"0 条"（0 条也是失败）。"""
    path = Path(path)
    if yaml is None:  # pragma: no cover
        raise RuntimeError("缺 pyyaml：judge 题面是 YAML，请先 uv sync")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_cases = payload.get("cases") or []
    cases: list[JudgeCase] = []
    for index, raw in enumerate(raw_cases, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"{path} 第 {index} 条不是映射")
        limit = raw.get("max_chars")
        cases.append(
            JudgeCase(
                id=str(raw["id"]),
                category=str(raw.get("category", "")),
                user=str(raw.get("user", "")),
                reply=str(raw.get("reply", "")),
                rubric=[str(item) for item in raw.get("rubric") or []],
                must_have=[str(item) for item in raw.get("must_have") or []],
                must_not_have=[str(item) for item in raw.get("must_not_have") or []],
                max_chars=int(limit) if limit else None,
                note=str(raw.get("note", "")),
            )
        )
    return cases


# --------------------------------------------------------------------- 离线打分
def score_offline(case: JudgeCase) -> tuple[int, list[str]]:
    """确定性 rubric：底 5 分，缺内容 / 踩红线各最多扣 2，超长扣 1（最低 1）。

    这里的口径**故意只覆盖可客观判断的部分**：语气、是否"接住情绪"、建议是否合理
    交给 ``--live``。同一条题面在两种模式下分数不可比，所以报告里写了 ``mode``。
    """
    reply = case.reply or ""
    score = SCORE_MAX
    reasons: list[str] = []

    missing = [item for item in case.must_have if item not in reply]
    if missing:
        score -= min(len(missing), MISSING_PENALTY_CAP)
        reasons.append(f"缺 must_have：{'、'.join(missing)}")

    forbidden = [item for item in case.must_not_have if item in reply]
    if forbidden:
        score -= min(len(forbidden), FORBIDDEN_PENALTY_CAP)
        reasons.append(f"命中 must_not_have：{'、'.join(forbidden)}")

    if case.max_chars and len(reply) > case.max_chars:
        score -= LENGTH_PENALTY
        reasons.append(f"超长：{len(reply)} 字 > 上限 {case.max_chars} 字")

    if not reasons:
        reasons.append("硬约束全过（离线只判 must_have / must_not_have / 长度，语气归 --live）")
    return max(SCORE_MIN, score), reasons


# --------------------------------------------------------------------- live 打分
def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里捞出第一个**平衡**的 JSON 对象。

    容错三种常见形态：裸 JSON、````` ```json ```` 围栏、JSON 前后带解释文字。
    不做"抓最外层最长的括号"——那样遇到嵌套就把两段拼坏了。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    start = raw.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(raw)):
            char = raw[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(raw[start : index + 1])
                    except ValueError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = raw.find("{", start + 1)
    return None


def parse_score(text: str) -> tuple[int, list[str]] | None:
    """解析 ``{score, reasons[]}``；返回 ``None`` 表示这条**判不出来**（计 0 并留痕）。"""
    payload = extract_json_object(text)
    if payload is None:
        return None
    raw_score = payload.get("score")
    try:
        score = int(round(float(raw_score)))
    except (TypeError, ValueError):
        return None
    reasons_raw = payload.get("reasons")
    if isinstance(reasons_raw, str):
        reasons = [reasons_raw]
    elif isinstance(reasons_raw, list):
        reasons = [str(item) for item in reasons_raw]
    else:
        reasons = []
    return min(SCORE_MAX, max(SCORE_MIN, score)), reasons


def build_judge_prompt(case: JudgeCase, reply: str) -> str:
    """把题面、候选回复、rubric 拼成裁判提示（``must_*`` 是 rubric 的客观下界）。"""
    lines = [f"# 用户说\n{case.user}", f"# 候选回复\n{reply}", "# rubric"]
    lines.extend(f"{index}. {item}" for index, item in enumerate(case.rubric, start=1))
    if case.must_have:
        lines.append("必须包含（缺一条即不满足 rubric）：" + "、".join(case.must_have))
    if case.must_not_have:
        lines.append("出现即扣分：" + "、".join(case.must_not_have))
    if case.max_chars:
        lines.append(f"长度上限：{case.max_chars} 字")
    lines.append("请输出 JSON。")
    return "\n\n".join(lines)


def _complete(provider: Any, request: ProviderRequest) -> Any:
    """同步调一次 ``provider.complete``（judge 是运维工具，不引入事件循环）。"""
    import asyncio

    return asyncio.run(provider.complete(request))


def judge_live(case: JudgeCase, provider: Any, *, timeout: float = 60.0) -> JudgeResult:
    """真模型判分：主模型先生成候选回复，judge 模型再按 rubric 打分。

    两次调用都用 ``temperature=0``（生成用一点温度，否则十条回复会长得一样）。
    任何一步失败都只影响这一条：``score=0`` + 原始输出留痕，不打断剩下九条。
    """
    try:
        generated = _complete(
            provider,
            ProviderRequest(
                role="main",
                system=[GENERATE_SYSTEM],
                messages=[user_message(case.user)],
                temperature=_GENERATE_TEMPERATURE,
                max_tokens=_GENERATE_MAX_TOKENS,
                timeout=timeout,
            ),
        )
        reply = str(getattr(generated, "text", "") or "")
    except Exception as exc:  # 生成失败：这条判不了，但要说清是哪一步
        return JudgeResult(
            id=case.id,
            category=case.category,
            score=0,
            reasons=[f"生成失败（{type(exc).__name__}: {exc}）"],
        )

    try:
        verdict = _complete(
            provider,
            ProviderRequest(
                role="judge",
                system=[JUDGE_SYSTEM],
                messages=[user_message(build_judge_prompt(case, reply))],
                temperature=0.0,
                max_tokens=_LIVE_MAX_TOKENS,
                timeout=timeout,
            ),
        )
        raw = str(getattr(verdict, "text", "") or "")
    except Exception as exc:
        return JudgeResult(
            id=case.id,
            category=case.category,
            score=0,
            reasons=[f"裁判调用失败（{type(exc).__name__}: {exc}）"],
            raw=reply,
        )

    parsed = parse_score(raw)
    if parsed is None:
        # 解析失败 = 计 0 + 留痕（PART-4 §7 的 J-01~J-10 最后一句）
        return JudgeResult(
            id=case.id,
            category=case.category,
            score=0,
            reasons=["解析失败：拿不到 {score, reasons[]}"],
            raw=raw,
        )
    score, reasons = parsed
    return JudgeResult(id=case.id, category=case.category, score=score, reasons=reasons, raw=reply)


# --------------------------------------------------------------------- 主入口
def run_cases(
    cases: Sequence[JudgeCase] | Path | str | None = None,
    *,
    mode: str = "offline",
    provider: Any = None,
    clock: Clock | None = None,
    report_dir: Path | str | None = None,
) -> JudgeReport:
    """跑完所有题面并给出报告；``report_dir`` 给了就落盘（CI 门禁不给，免得噪声）。"""
    if mode not in {"offline", "live"}:
        raise ValueError(f"mode 只能是 offline / live，收到 {mode!r}")
    if cases is None:
        selected = load_cases(DEFAULT_CASES)
        cases_path = str(DEFAULT_CASES)
    elif isinstance(cases, (str, Path)):
        cases_path = str(cases)
        selected = load_cases(cases)
    else:
        selected = list(cases)
        cases_path = str(DEFAULT_CASES)
    if mode == "live" and provider is None:
        raise ValueError("live 模式需要 provider（judge 与 main 是两次真调用）")

    results: list[JudgeResult] = []
    for case in selected:
        if mode == "offline":
            score, reasons = score_offline(case)
            results.append(JudgeResult(case.id, case.category, score, reasons))
        else:
            results.append(judge_live(case, provider))

    report = JudgeReport(
        mode=mode,
        results=results,
        cases_path=cases_path,
        generated_at=to_local_iso((clock or SystemClock()).now()),
    )
    if report_dir is not None:
        write_report(report, report_dir, clock=clock)
    return report


def write_report(
    report: JudgeReport, report_dir: Path | str, *, clock: Clock | None = None
) -> Path:
    """落盘 ``judge-YYYYMMDD.json``：分数、理由、解析失败的原始输出都在这儿。"""
    day: date = (clock or SystemClock()).now().astimezone().date()
    path = Path(report_dir) / f"judge-{day:%Y%m%d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python evals/judge/run_judge.py",
        description="judge 评测：10 条 rubric 的均分（离线基线；--live 走真模型）",
    )
    parser.add_argument("--live", action="store_true", help="连真实模型生成回复并判分")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="题面文件（默认 cases.yaml）")
    parser.add_argument(
        "--report-dir", default=str(DEFAULT_REPORT_DIR), help="报告落盘目录（data 之外）"
    )
    parser.add_argument("--env-file", default=".env", help="配置文件路径（--live 用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    from yixiang.console import force_utf8_stdio

    force_utf8_stdio()  # 报告里有中文与 ✓/⚠：重定向时不能让 GBK 编码崩掉
    args = build_parser().parse_args(argv)
    provider = None
    if args.live:
        from yixiang.config import Settings
        from yixiang.ops.usage import JsonlUsageSink
        from yixiang.providers import OpenAICompatibleProvider

        settings = Settings.load(env_file=args.env_file)
        clock = SystemClock()
        if not settings.api_key:
            print(
                "live 模式需要 YIXIANG_API_KEY（离线基线不需要，直接跑不带 --live 的版本）",
                file=sys.stderr,
            )
            return 1
        provider = OpenAICompatibleProvider(
            settings, usage_sink=JsonlUsageSink(settings.usage_path, clock=clock), clock=clock
        )
    report = run_cases(
        args.cases,
        mode="live" if args.live else "offline",
        provider=provider,
        report_dir=args.report_dir,
    )
    print(report.render())
    if report.failed:
        # 解析失败必须退出非零：它意味着"这一条根本没被判"，不是"分数低"
        print(f"⚠️  {len(report.failed)} 条判分失败，原始输出已写进报告目录", file=sys.stderr)
        return 1
    return 0 if report.mean >= PASS_MEAN else 1


if __name__ == "__main__":
    raise SystemExit(main())
