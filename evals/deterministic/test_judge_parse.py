"""judge 判词的解析容错（``run_judge.py`` 的 ``parse_verdict``）。

第一次真跑 ``--live``（生成走 DeepSeek、裁判走本机 qwen3.5）时，十条里出了两条"判分
失败"，都是**格式问题、不是判断问题**：

  * J-01——JSON 内容全对，收尾却把 ``}]`` 写成了 ``]]``、还漏了 ``}``；
  * J-02——把候选回复原文的引号直接抄进了 ``reasons``，JSON 根本不成形。

两种都记 0 + "拿不到 ``{score, reasons[]}``"，等于把"模型没答"和"格式写歪"混成了同一
件事。这里把三者分开钉住：内容在、只是括号歪 → 修回来**并留痕**（``repaired``）；
连 JSON 都不是 → 带叮嘱**重问一次**（``retried``）；仍然不行 → 照老规矩计 0。
全程离线：一次真模型都不连（§13.6）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from yixiang.ops.release_gate import _load_judge

# J-01 的原始输出，一字不改（evals/judge/reports/judge-20260921.json 的 raw 字段）：
# 内容全对，收尾却把 ``}]`` 写成了 ``]]``、还漏了 ``}``。
J01_VERDICT = """{
"score": 5,
"reasons": ["简短回应，符合字数限制", "未调用工具", "包含必选词“在”", "无扣分项词汇", "风格自然"]]"""

# J-02 的原始输出，一字不改（同一天的 raw 字段）：模型把候选回复原文的引号直接抄进了
# ``reasons``，JSON 不成形——括号配平也救不回来，只能叮嘱后重问一次。
J02_VERDICT = """{
"score": 4,
"reasons": ["成功接住闲聊调性，回复简短有趣，符合冷笑话风格", "未编造用户偏好，也未出现扣分短语", "长度在 200 字以内", "包含了必含项 Oct 31（隐含在日期语境或需显式检查，此处假设回复中已包含或视为满足，若严格看回复文本“有一天，0 对 8 说：胖就胖吧，还系什么腰带。”并未显式出现字符串"Oct 31"，则可能缺此项。但根据常见评测逻辑，若回复极短且未显式包含，可能扣分。重新审视：回复文本确实没有"Oct 31"。", "缺一项必含项：Oct 31"]
}"""

GENERATED = "在的，我在。"


class _StubProvider:
    """只回写死文本的假 provider：生成走 main、判词按顺序取（用光就重发最后一条）。"""

    def __init__(self, *verdicts: str) -> None:
        self.verdicts = list(verdicts)
        self.roles: list[str] = []
        self.asked: list[str] = []  # 每一发裁判 prompt：用来钉"重试时叮嘱了什么"

    async def complete(self, request: SimpleNamespace) -> SimpleNamespace:
        self.roles.append(request.role)
        if request.role == "main":
            return SimpleNamespace(text=GENERATED)
        self.asked.append(request.messages[0].content)
        index = min(len(self.asked) - 1, len(self.verdicts) - 1)
        return SimpleNamespace(text=self.verdicts[index])


def test_a_stray_closing_bracket_and_a_missing_brace_are_repaired(repo_root: Path):
    """J-01 现场：五个理由一条不少地捞回来，并标出"修过括号"。"""
    module = _load_judge(repo_root)

    verdict = module.parse_verdict(J01_VERDICT)

    assert verdict is not None
    assert (verdict.score, len(verdict.reasons), verdict.repaired) == (5, 5, True)


def test_a_missing_closing_brace_alone_is_closed(repo_root: Path):
    """只少一个 ``}``：内容同样完整，按栈补齐。"""
    module = _load_judge(repo_root)

    verdict = module.parse_verdict('{"score": 3, "reasons": ["短"]')

    assert verdict is not None
    assert (verdict.score, verdict.reasons, verdict.repaired) == (3, ["短"], True)


def test_a_well_formed_verdict_is_not_marked_as_repaired(repo_root: Path):
    """正常的输出一个字符都不许改——``repaired`` 是留痕，不是常态。"""
    module = _load_judge(repo_root)

    verdict = module.parse_verdict('{"score": 4, "reasons": ["切题", "语气合适"]}')

    assert verdict is not None
    assert (verdict.score, verdict.reasons, verdict.repaired) == (4, ["切题", "语气合适"], False)


def test_fenced_and_prose_wrapped_verdicts_still_parse(repo_root: Path):
    """围栏与前后解释文字是常见形态，两种都不算"修过"。"""
    module = _load_judge(repo_root)

    fenced = module.parse_verdict('```json\n{"score": 5, "reasons": ["好"]}\n```')
    prose = module.parse_verdict('我的判断如下：\n{"score": 2, "reasons": ["跑题"]}\n请过目。')

    assert fenced is not None and (fenced.score, fenced.repaired) == (5, False)
    assert prose is not None and (prose.score, prose.repaired) == (2, False)


def test_a_verdict_without_any_object_is_still_a_failure(repo_root: Path):
    """真没答（只有自然语言、连 ``{`` 都没有）→ 依旧判不出来，不许假装有分。"""
    module = _load_judge(repo_root)

    assert module.parse_verdict("这条回复挺好的，我给 5 分。") is None
    assert module.parse_verdict("") is None


def test_the_repair_leaves_a_trace_in_the_live_result(repo_root: Path):
    """修复要**看得见**：理由里带一句说明，否则报告里分不清它是怎么来的。"""
    module = _load_judge(repo_root)
    case = module.load_cases(module.DEFAULT_CASES)[0]
    provider = _StubProvider(J01_VERDICT)

    result = module.judge_live(case, provider)

    assert provider.roles == ["main", "judge"]
    assert result.score == 5
    assert result.reasons[0] == module.REPAIRED_NOTE
    assert len(result.reasons) == 6  # 1 条留痕 + 模型给的 5 条理由
    assert result.raw == GENERATED


def test_a_verdict_that_is_not_even_json_is_asked_once_more(repo_root: Path):
    """J-02 现场：引号抄进 ``reasons`` → JSON 不成形，叮嘱一句再问一次。"""
    module = _load_judge(repo_root)
    case = module.load_cases(module.DEFAULT_CASES)[1]
    provider = _StubProvider(J02_VERDICT, '{"score": 4, "reasons": ["切题"]}')

    result = module.judge_live(case, provider)

    assert provider.roles == ["main", "judge", "judge"]  # 生成一发 + 判两发
    assert result.score == 4
    assert result.reasons == [module.RETRY_NOTE, "切题"]
    # 第二发要带叮嘱（两句不许一样），否则同一个模型只会照原样再错一次
    assert "只输出一个 JSON 对象" in provider.asked[1]
    assert provider.asked[1] != provider.asked[0]
    assert provider.asked[0].startswith(provider.asked[1][:20])  # 重试是在原题面上加叮嘱


def test_two_unparseable_verdicts_still_count_as_a_failure(repo_root: Path):
    """两次都解析不出来 = 判不出来：计 0、留原始输出，不许硬凑一个分。"""
    module = _load_judge(repo_root)
    case = module.load_cases(module.DEFAULT_CASES)[1]
    provider = _StubProvider(J02_VERDICT)

    result = module.judge_live(case, provider)

    assert provider.roles == ["main", "judge", "judge"]
    assert result.score == 0
    assert result.reasons == ["解析失败：拿不到 {score, reasons[]}"]
    assert result.raw == J02_VERDICT
