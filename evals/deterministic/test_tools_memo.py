"""D-01 备忘触发 + 相对时间解析（PART-1 §7、TECH §9.2）。

铁律：**日期解析在工具侧做，不在模型侧**——模型只负责把"周五中午"原样传进来。
解析错了会被用例抓住；模型自己算错了就只能靠运气。

固定时钟 2026-09-19（周六）10:00 +08:00，所以：

  * "周五中午"  = 下一个周五 = 2026-09-25T12:00
  * "下周五"    = 2026-10-02
  * "周三"      = 2026-09-23（本周三已过 → 自然顺延）
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fake_provider import FakeProvider, text_reply, tool_round

from yixiang.tools.memo import parse_when


def test_d01_memo_call_parses_relative_due_at(registry, session, conn, turn):
    provider = FakeProvider(
        tool_round(("add_memo", {"content": "交材料", "due_at": "周五中午"})),
        text_reply("好，周五中午前交材料，我记下了。"),
    )
    result = turn(session, registry, provider, "记一下周五中午前交材料")

    # ① 请求侧：模型拿到工具清单与精确到分钟的本轮时间，且时间在 system 末尾
    first, second = provider.requests
    assert "add_memo" in [item["function"]["name"] for item in first.tools]
    assert first.messages[0].content == "记一下周五中午前交材料"
    assert "当前时间：2026-09-19 10:00" in first.system[6]
    assert first.system_text().endswith(first.system[6])
    # 工具结果原样回喂（第二轮请求末尾是 tool 消息，带着解析后的 ISO 时间）
    assert second.messages[-1].role == "tool"
    assert "2026-09-25T12:00:00+08:00" in second.messages[-1].content

    # ② 行为侧：该调的调了，且只调一次；人话原样进工具
    assert [event.tool for event in result.tool_calls] == ["add_memo"]
    assert result.tool_calls[0].ok is True
    assert result.tool_calls[0].args["due_at"] == "周五中午"

    # ③ 结果侧：DB 里存的是解析后的 ISO8601，回复就是模型那句话
    row = conn.execute("SELECT id, content, due_at, done FROM memos").fetchone()
    assert row["content"] == "交材料" and row["done"] == 0
    assert row["due_at"] == "2026-09-25T12:00:00+08:00"
    assert json.loads(result.tool_calls[0].output)["id"] == row["id"]
    assert result.finish_reason == "stop" and result.error is None


def test_memo_idempotency_key_dedupes_repeat_delivery(registry, conn):
    first = registry.run("add_memo", {"content": "取快递", "idempotency_key": "qq-msg-1"})
    again = registry.run("add_memo", {"content": "取快递", "idempotency_key": "qq-msg-1"})
    other = registry.run("add_memo", {"content": "取快递", "idempotency_key": "qq-msg-2"})

    assert first.ok and again.ok and other.ok
    assert json.loads(again.output)["deduped"] is True
    assert json.loads(again.output)["id"] == json.loads(first.output)["id"]
    assert json.loads(other.output)["id"] != json.loads(first.output)["id"]
    assert conn.execute("SELECT COUNT(*) FROM memos").fetchone()[0] == 2


def test_memo_list_and_finish_round_trip(registry):
    soon = registry.run("add_memo", {"content": "交材料", "due_at": "2026-09-19T12:00"})
    later = registry.run("add_memo", {"content": "买猫粮"})
    assert soon.ok and later.ok

    listed = registry.run("list_memos", {})
    lines = listed.output.splitlines()
    assert listed.ok is True
    assert len(lines) == 2
    assert lines[0].endswith("交材料") and "2026-09-19T12:00:00+08:00" in lines[0]
    assert lines[1].endswith("买猫粮") and "无截止时间" in lines[1]

    done = registry.run("finish_memo", {"id": json.loads(soon.output)["id"]})
    assert done.ok is True and json.loads(done.output) == {"ok": True, "id": 1}
    open_now = registry.run("list_memos", {})
    assert "买猫粮" in open_now.output and "交材料" not in open_now.output
    assert "交材料" in registry.run("list_memos", {"status": "done"}).output

    missing = registry.run("finish_memo", {"id": 9999})
    assert missing.ok is False
    assert missing.output.startswith("Error") and "list_memos" in missing.output


def test_unparseable_due_at_degrades_to_no_deadline_with_a_warning(registry, conn):
    outcome = registry.run("add_memo", {"content": "随便写写", "due_at": "   "})
    assert outcome.ok is True  # 降级保存，不算工具失败
    payload = json.loads(outcome.output)
    assert payload["due_at"] is None and "warning" in payload
    assert conn.execute("SELECT due_at FROM memos").fetchone()["due_at"] is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-09-25", "2026-09-25T09:00:00+08:00"),
        ("2026-09-25T12:00", "2026-09-25T12:00:00+08:00"),
        ("明天", "2026-09-20T09:00:00+08:00"),
        ("后天晚上", "2026-09-21T20:00:00+08:00"),
        ("周五中午", "2026-09-25T12:00:00+08:00"),
        ("周三", "2026-09-23T09:00:00+08:00"),
        ("下周五", "2026-10-02T09:00:00+08:00"),
        ("今天18:00", "2026-09-19T18:00:00+08:00"),
        ("15:30", "2026-09-19T15:30:00+08:00"),
        ("3点", "2026-09-19T15:00:00+08:00"),
        ("晚上8点", "2026-09-19T20:00:00+08:00"),
        ("上午", "2026-09-20T09:00:00+08:00"),  # 没写日期又说过去的时刻 → 明天
    ],
)
def test_relative_time_table(clock, text, expected):
    parsed = parse_when(text, clock.now())
    assert parsed is not None
    assert parsed.isoformat(timespec="seconds") == expected
    assert parsed.utcoffset() == timedelta(hours=8)


def test_empty_time_text_means_no_deadline(clock):
    assert parse_when("", clock.now()) is None
