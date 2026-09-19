"""检索门控（D-09、D-10、D-16）+ `gate.jsonl` 门禁（§7.5.3）。

离线两条腿：

  * **规则层**（确定性，真测）——寒暄 skip / 记忆词 retrieve / 模糊交模型；
  * **模型层**——离线用"自我指涉词"代理（真模型的召回只会更好），
    真实模型的指标由 ``@pytest.mark.live`` 那条用例在 nightly 里测。

漏检率必须为 0：漏检 = 失忆，是产品级事故；误检只多花一点 token，容忍 30%。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fake_provider import FakeProvider, text_reply

from yixiang import memory
from yixiang.app import App
from yixiang.errors import E_GATE_FAIL_OPEN
from yixiang.memory import gate, semantic
from yixiang.runtime.session import SessionManager

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "gate.jsonl"

# 离线代理模型的判据：出现自我指涉/时间指代/记忆动词就认为需要翻记忆。
# 它只是"下界"——真实模型（live 用例）只会比它准，所以它不变绿就说明规则层坏了。
SELF_HINTS = (
    "我",
    "咱",
    "上次",
    "之前",
    "记得",
    "忘了",
    "喜欢",
    "偏好",
    "习惯",
    "计划",
    "项目",
    "生日",
    "过敏",
    "住在",
    "工作",
)


def _load_golden() -> list[dict]:
    return [
        json.loads(line)
        for line in GOLDEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _offline_gate(message: str) -> tuple[gate.GateDecision, bool]:
    """返回 ``(decision, 是否由规则直接判定)``。"""
    decision = gate.rule_decision(message)
    if decision is not None:
        return decision, True
    hit = any(hint in message for hint in SELF_HINTS)
    return gate.GateDecision(hit, message if hit else "", "代理模型", "model"), False


def _metrics(rows: list[dict]) -> dict[str, float]:
    total = len(rows)
    positives = sum(1 for row in rows if row["should_retrieve"])
    negatives = total - positives
    missed = false_alarms = rule_skipped = 0
    for row in rows:
        decision, by_rule = _offline_gate(row["message"])
        if by_rule and not decision.retrieve:
            rule_skipped += 1
        if decision.retrieve and not row["should_retrieve"]:
            false_alarms += 1
        if not decision.retrieve and row["should_retrieve"]:
            missed += 1
    return {
        "total": total,
        "positives": positives,
        "miss_rate": missed / positives if positives else 0.0,
        "false_alarm_rate": false_alarms / negatives if negatives else 0.0,
        "rule_skip_rate": rule_skipped / total if total else 0.0,
    }


class BoomProvider:
    """门控自己的模型炸了：超时 / 限流 / 鉴权失败都长这样。"""

    async def complete(self, request):  # noqa: ARG002 - 只关心抛异常
        raise RuntimeError("gate timeout")


# ------------------------------------------------------------------ D-09
def test_d09_rule_layer_skips_greetings_and_passes_math_to_model():
    greeting = gate.rule_decision("你好")
    assert greeting is not None
    assert greeting.retrieve is False
    assert greeting.source == "rule"
    assert gate.rule_decision("嗯嗯").retrieve is False  # 去空白后 ≤6 字
    assert gate.rule_decision("1+1=?") is None  # 数学：不是确定性 skip，交模型
    assert gate.rule_decision("帮我解释 TCP 三次握手") is None


def test_d09_model_says_false_means_no_retrieval():
    provider = FakeProvider(
        text_reply('思考一下……{"retrieve": false, "query": "", "reason": "数学"}')
    )

    decision = asyncio.run(gate.should_retrieve("1+1=?", provider))

    assert decision.retrieve is False
    assert decision.query == ""
    assert decision.source == "model"
    assert len(provider.requests) == 1
    assert provider.requests[0].role == "gate"
    assert provider.requests[0].max_tokens == gate.GATE_MAX_TOKENS


def test_d09_skipped_turn_injects_no_retrieval_block(settings, conn, clock):
    session = SessionManager(settings, store=conn, session_id="cli:test", clock=clock)
    session.begin_turn("1+1=?")
    session.prime_retrieval("1+1=?", allowed=False)

    blocks = session.system_blocks()

    assert blocks[5] == ""  # S6 空
    assert not any("与本次提问相关的记忆" in block for block in blocks)


# ------------------------------------------------------------------ D-10
def test_d10_rule_hit_retrieves_without_calling_the_model():
    provider = FakeProvider()  # 剧本为空：只要被调用就会炸

    decision = asyncio.run(gate.should_retrieve("我上周说喜欢什么来着", provider))

    assert decision.retrieve is True
    assert decision.source == "rule"
    assert decision.query == "我上周说喜欢什么来着"
    assert provider.requests == []


def test_d10_hit_injects_the_matching_fact(settings, conn, clock):
    app = App.from_settings(
        settings,
        provider=FakeProvider(text_reply("你喜欢看 NBA 篮球比赛")),
        clock=clock,
        embedder=semantic.HashEmbedder(),
    )
    try:
        assert json.loads(
            app.registry.execute(
                "save_memory", {"subject": "偏好", "content": "用户喜欢看 NBA 篮球比赛"}
            )
        )["action"] == "insert"

        result = app.ask("我上周说喜欢什么来着", stream=False)

        # ``system`` 是分段列表（§4.1），不进 messages
        blocks = app.provider.requests[-1].system
        assert any("用户喜欢看 NBA 篮球比赛" in block for block in blocks)
        assert result.gate["source"] == "rule"
        assert result.gate["retrieve"] is True
    finally:
        app.close()
        memory.reset()


# ------------------------------------------------------------------ D-16
def test_d16_gate_failure_fails_open_with_error_code():
    decision = asyncio.run(gate.should_retrieve("推荐点什么", BoomProvider()))

    assert decision.retrieve is True  # fail-open：宁可多检索
    assert decision.source == "fail_open"
    assert decision.query == "推荐点什么"
    assert decision.as_trace()["error"] == E_GATE_FAIL_OPEN


def test_d16_fail_open_still_retrieves_for_the_user(settings, conn, clock):
    app = App.from_settings(
        settings,
        provider=FakeProvider(text_reply("推荐你看《大明王朝 1566》")),
        clock=clock,
        embedder=semantic.HashEmbedder(),
    )
    try:
        app.registry.execute(
            "save_memory", {"subject": "偏好", "content": "用户喜欢历史题材的剧"}
        )
        app._provider_injected = False  # 让门控模型参与（注入的假模型默认不参与门控）
        app.gate_provider = BoomProvider()

        result = app.ask("推荐点什么", stream=False)

        blocks = app.provider.requests[-1].system
        assert any("用户喜欢历史题材的剧" in block for block in blocks)  # 用户无感
        assert result.gate["source"] == "fail_open"
        assert result.gate["error"] == E_GATE_FAIL_OPEN
        assert app.last_record["gate"]["error"] == E_GATE_FAIL_OPEN  # trace 留痕
    finally:
        app.close()
        memory.reset()


# ------------------------------------------------------------------ 解析容错
@pytest.mark.parametrize(
    "raw",
    [
        '{"retrieve": true, "query": "篮球", "reason": "偏好"}',
        '```json\n{"retrieve": true, "query": "篮球", "reason": "偏好"}\n```',
        '嗯，{"retrieve": true, "query": "篮球", "reason": "偏好"}，就这些',
    ],
)
def test_parse_decision_tolerates_wrappers(raw):
    decision = gate.parse_decision(raw)
    assert decision is not None
    assert decision.retrieve is True
    assert decision.query == "篮球"


@pytest.mark.parametrize("raw", ["", "我想想", "没有 JSON 的思考块", '{"query": "x"}'])
def test_parse_decision_returns_none_without_a_usable_answer(raw):
    """没有 `{` 是"模型没给答案"，不是"不需要检索"——由调用方 fail-open。"""
    assert gate.parse_decision(raw) is None


# ------------------------------------------------------------------ 标注集门禁
def test_gate_golden_set_is_large_enough_and_balanced():
    rows = _load_golden()
    positives = sum(1 for row in rows if row["should_retrieve"])

    assert len(rows) >= 40
    assert positives >= 15
    assert len(rows) - positives >= 10
    for row in rows:
        assert isinstance(row["message"], str) and row["message"]
        assert isinstance(row["should_retrieve"], bool)
        assert row["note"]


def test_gate_golden_set_meets_the_three_gates():
    metrics = _metrics(_load_golden())

    assert metrics["miss_rate"] == 0  # 漏检率 = 0，硬门禁
    assert metrics["false_alarm_rate"] <= 0.30
    assert metrics["rule_skip_rate"] >= 0.15


@pytest.mark.live
def test_gate_golden_set_with_the_real_model():
    """nightly / 发版前：用真模型跑同一份标注集（默认不进 PR 门禁）。"""
    pytest.importorskip("httpx")
    from yixiang.config import Settings
    from yixiang.providers import OpenAICompatibleProvider

    settings = Settings.load()
    if not settings.api_key:
        pytest.skip("没有 API key：跳过真实门控评测")
    provider = OpenAICompatibleProvider(settings)

    async def run() -> list[dict[str, float]]:
        missed = false_alarms = rule_skipped = 0
        rows = _load_golden()
        for row in rows:
            decision = await gate.should_retrieve(row["message"], provider)
            if decision.source == "rule" and not decision.retrieve:
                rule_skipped += 1
            if decision.retrieve and not row["should_retrieve"]:
                false_alarms += 1
            if not decision.retrieve and row["should_retrieve"]:
                missed += 1
        positives = sum(1 for row in rows if row["should_retrieve"])
        negatives = len(rows) - positives
        return [
            {
                "miss_rate": missed / positives,
                "false_alarm_rate": false_alarms / negatives,
                "rule_skip_rate": rule_skipped / len(rows),
            }
        ]

    metrics = asyncio.run(run())[0]
    assert metrics["miss_rate"] == 0
    assert metrics["false_alarm_rate"] <= 0.30
    assert metrics["rule_skip_rate"] >= 0.15
