"""Provider 层：角色路由、重试矩阵、usage 记账、"假流"分片组装（PART-1 §7）。

全程离线：假 HTTP 传输按剧本回放状态码，假 sleep 只记录退避秒数——
用例里一次 ``sleep`` 都不真发生、一次真模型都不连（§13.6）。

断言顺序固定：① 请求侧（模型看到了什么）→ ② 行为侧（重试 / 降级发生了没）
→ ③ 结果侧（记账与最终回复）。
"""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import TURN_ID
from fake_provider import (
    SleepRecorder,
    completion_body,
    delta_finish,
    delta_text,
    delta_tool,
    run_complete,
    run_stream,
    scripted_transport,
    stream_body,
)

from yixiang.config import Settings
from yixiang.errors import E_LLM_AUTH, E_LLM_BAD_REQUEST, E_LLM_TIMEOUT, ProviderError
from yixiang.ops.pricing import PRICES
from yixiang.ops.usage import CollectingUsageSink, JsonlUsageSink, iter_lines
from yixiang.providers import OpenAICompatibleProvider
from yixiang.runtime.models import (
    ProviderRequest,
    collecting_observer,
    user_message,
)

# 一份固定请求：静态段在前、时间在 system 末尾（§4.5 前缀缓存）
SYSTEM = ["静态段 S1", "当前时间：2026-09-19 10:00（周六，UTC+08:00）"]


def provider_for(settings, steps, *, usage_sink=None):
    """把剧本装进假传输，返回 ``(provider, request_bodies, sleep_recorder)``。"""
    transport, bodies = scripted_transport(*steps)
    recorder = SleepRecorder()
    provider = OpenAICompatibleProvider(
        settings,
        transport=transport,
        usage_sink=usage_sink,
        sleep=recorder,
        jitter=0.0,  # 关抖动：退避秒数才是确定的
    )
    return provider, bodies, recorder


def request_for(settings, *, role: str = "main") -> ProviderRequest:
    return ProviderRequest(
        role=role,
        system=list(SYSTEM),
        messages=[user_message("你好")],
        turn_id=TURN_ID,
        session_id="cli:test",
        timeout=1.0,
    )


# ------------------------------------------------------------------ 角色路由
def test_role_routing_table_falls_back_to_the_main_model(settings):
    # 故意写一个 ≠ 默认值（deepseek-flash）的名字：这样"路由读的是设置、不是默认值"才被证明。
    # 用 deepseek-v4-pro：官网在售、不是退役别名（退役名字已经有守门用例盯着了）
    settings.main_model = "deepseek-v4-pro"
    settings.gate_model = ""
    settings.judge_model = ""
    settings.utility_model = ""
    assert settings.model_for("main") == "deepseek-v4-pro"
    assert settings.model_for("gate") == "deepseek-v4-pro"
    assert settings.model_for("judge") == "deepseek-v4-pro"
    assert settings.model_for("utility") == "deepseek-v4-pro"
    assert settings.model_for("embed") == settings.embed_model

    settings.gate_model = "glm-4-flash"
    settings.judge_model = "deepseek-v4-pro"
    assert settings.model_for("gate") == "glm-4-flash"
    assert settings.model_for("utility") == "deepseek-v4-pro"  # 窄角色留空 → 回落
    with pytest.raises(ValueError):
        settings.model_for("nope")


def test_default_main_model_is_the_model_we_actually_use():
    # 只使用 deepseek-flash：默认值必须就是它，否则 .env 漏配时会悄悄换成别的模型
    assert Settings().main_model == "deepseek-flash"


def test_default_main_model_has_its_own_price_row():
    # 默认模型在价目表里有名有姓，成本才不会静默按兜底价计（偏乐观）
    assert Settings().main_model in PRICES


def test_role_routing_picks_the_model_and_writes_one_usage_line(settings):
    settings.gate_model = "glm-4-flash"
    sink = CollectingUsageSink()
    body = completion_body(
        "pong",
        model="glm-4-flash",
        usage_raw={
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "prompt_cache_hit_tokens": 200,
        },
    )
    provider, bodies, _ = provider_for(settings, [body], usage_sink=sink)
    reply = run_complete(provider, request_for(settings, role="gate"))

    # ① 请求侧：出站体是 OpenAI 兼容格式，模型由角色路由决定
    assert bodies[0]["model"] == "glm-4-flash"
    assert bodies[0]["stream"] is False
    assert bodies[0]["messages"][0] == {
        "role": "system",
        "content": "静态段 S1\n\n当前时间：2026-09-19 10:00（周六，UTC+08:00）",
    }
    assert bodies[0]["messages"][1] == {"role": "user", "content": "你好"}
    assert "tools" not in bodies[0]  # 没有工具就不下发 tools
    # ② 行为侧：一次调用就结束，没有多余请求
    assert len(bodies) == 1
    # ③ 结果侧：回复、token 与成本都落在一行 usage 上
    assert reply.text == "pong" and reply.finish_reason == "stop"
    assert reply.usage.input_tokens == 1000
    assert reply.usage.cached_input_tokens == 200
    assert reply.usage.output_tokens == 500
    line = sink.lines[-1]
    assert (line.role, line.model, line.turn_id) == ("gate", "glm-4-flash", TURN_ID)
    # 价目按**实际路由到的模型**取值：glm-4-flash = (0, 0, 0) 元/百万 → 这行必须是 0 元。
    # 这个 0 是有判别力的：若哪天写死成默认价 / 兜底价（deepseek-flash 的 2 / 0.04 / 8），
    # 同样这批 token（1000 进 / 500 出）就不会是 0。
    assert line.cost_cny == 0.0
    expected = (800 * 0.0 + 200 * 0.0 + 500 * 0.0) / 1e6
    assert line.cost_cny == pytest.approx(expected)


def test_usage_jsonl_writes_the_frozen_fields(settings, tmp_path, clock):
    path = tmp_path / "usage.jsonl"
    sink = JsonlUsageSink(path, clock=clock)
    provider, _, _ = provider_for(settings, [completion_body("好")], usage_sink=sink)
    run_complete(provider, request_for(settings))

    raw = json.loads(path.read_text(encoding="utf-8").strip())
    assert set(raw) == {
        "ts",
        "turn_id",
        "role",
        "model",
        "input",
        "cached_input",
        "output",
        "latency_ms",
        "cost_cny",
    }
    assert raw["ts"] == "2026-09-19T10:00:00+08:00"
    assert raw["turn_id"] == TURN_ID and raw["role"] == "main"
    assert [line.turn_id for line in iter_lines(path)] == [TURN_ID]


# ------------------------------------------------------------------ 重试矩阵
@pytest.mark.parametrize(
    ("steps", "expected_sleeps"),
    [
        ([500, completion_body("恢复")], [0.5]),
        ([500, 503, completion_body("恢复")], [0.5, 1.5]),
        ([429, 429, completion_body("恢复")], [2.0, 6.0]),
        ([httpx.ReadTimeout("超时"), completion_body("恢复")], [0.5]),
        ([httpx.ConnectError("断网"), completion_body("恢复")], [0.5]),
    ],
)
def test_retry_matrix_backs_off_then_succeeds(settings, steps, expected_sleeps):
    provider, bodies, recorder = provider_for(settings, steps)
    reply = run_complete(provider, request_for(settings))

    # ① 请求侧：重试就是同一个请求体再发一次
    assert all(body["model"] == settings.main_model for body in bodies)
    # ② 行为侧：退避秒数逐条对上矩阵（0.5/1.5 · 2.0/6.0 · 0.5）
    assert recorder.calls == expected_sleeps
    assert len(bodies) == len(steps)
    # ③ 结果侧：最终拿到的是成功那一次的回复
    assert reply.text == "恢复" and reply.error is None


def test_retry_budget_is_finite_and_names_the_error_code(settings):
    provider, bodies, recorder = provider_for(
        settings, [500, 500, 500, completion_body("不该用到")]
    )
    with pytest.raises(ProviderError) as excinfo:
        run_complete(provider, request_for(settings))

    assert excinfo.value.code == E_LLM_TIMEOUT
    assert excinfo.value.retryable is True
    assert len(bodies) == 3  # 1 次 + 2 次重试
    assert recorder.calls == [0.5, 1.5]


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, E_LLM_BAD_REQUEST), (401, E_LLM_AUTH), (403, E_LLM_AUTH), (422, E_LLM_BAD_REQUEST)],
)
def test_client_errors_are_not_retried(settings, status, code):
    provider, bodies, recorder = provider_for(settings, [status, completion_body("不该用到")])
    with pytest.raises(ProviderError) as excinfo:
        run_complete(provider, request_for(settings))

    assert excinfo.value.code == code
    assert excinfo.value.retryable is False
    assert recorder.calls == []  # 4xx 不退避
    assert len(bodies) == 1  # 也不重发


def test_api_key_never_leaks_into_the_error_text(settings):
    settings.api_key = "sk-secret-1234567890"
    provider, _, _ = provider_for(settings, [401])
    request = request_for(settings)
    with pytest.raises(ProviderError) as excinfo:
        run_complete(provider, request)
    assert provider.redact(f"命中 {settings.api_key} 的报错") == "命中 *** 的报错"
    assert settings.api_key not in str(excinfo.value)


# -------------------------------------------------------------------- 假流
def test_stream_assembles_deltas_and_reports_usage(settings):
    steps = [
        stream_body(
            delta_text("你"),
            delta_text("好"),
            delta_finish("stop", usage_raw={"prompt_tokens": 12, "completion_tokens": 3}),
        )
    ]
    provider, bodies, _ = provider_for(settings, steps)
    events = []
    reply = run_stream(provider, request_for(settings), collecting_observer(events))

    # ① 请求侧：流式请求带 include_usage
    assert bodies[0]["stream"] is True
    assert bodies[0]["stream_options"] == {"include_usage": True}
    assert len(bodies) == 1
    # ② 行为侧：每个分片都作为 text_delta 事件吐给 observer
    assert [event.data["text"] for event in events] == ["你", "好"]
    assert [event.kind for event in events] == ["text_delta", "text_delta"]
    # ③ 结果侧：拼装后的整段文本与 usage
    assert reply.text == "你好" and reply.finish_reason == "stop"
    assert reply.usage.input_tokens == 12 and reply.usage.output_tokens == 3
    assert reply.error is None


def test_stream_assembles_sharded_tool_calls_and_revokes_the_preview(settings):
    steps = [
        stream_body(
            delta_text("我先查一下："),
            delta_tool(0, call_id="call_a", name="list_today"),
            delta_tool(0, arguments='{"date":'),
            delta_tool(0, arguments=' "2026-09-19"}'),
            delta_tool(1, call_id="call_b", name="add_memo", arguments='{"content": "交材料"}'),
            delta_finish("tool_calls"),
        )
    ]
    provider, bodies, _ = provider_for(settings, steps)
    events = []
    reply = run_stream(provider, request_for(settings), collecting_observer(events))

    assert len(bodies) == 1
    # 分片按 index 拼装，参数跨分片也要拼回合法 JSON
    assert [call.name for call in reply.tool_calls] == ["list_today", "add_memo"]
    assert reply.tool_calls[0].id == "call_a"
    assert reply.tool_calls[0].arguments == {"date": "2026-09-19"}
    assert reply.tool_calls[1].arguments == {"content": "交材料"}
    assert reply.finish_reason == "tool_calls"
    # 这一轮其实是工具轮 → 撤回已经打出去的预览（§5.4 边界 1）
    assert [event.kind for event in events] == ["text_delta", "text_revoke"]
    assert events[-1].data["chars"] == len("我先查一下：")


def test_stream_failure_without_text_falls_back_to_non_stream(settings):
    steps = [
        stream_body(delta_text("半句"), fail_after=0),
        completion_body("完整的回复"),
    ]
    provider, bodies, _ = provider_for(settings, steps)
    events = []
    reply = run_stream(provider, request_for(settings), collecting_observer(events))

    # 一个分片都没吐出去 → 降级为非流式（重试矩阵由 complete 自带）
    assert bodies[0]["stream"] is True
    assert bodies[1]["stream"] is False
    assert events == []
    assert reply.text == "完整的回复" and reply.error is None


def test_stream_failure_after_text_keeps_what_the_user_already_saw(settings):
    steps = [stream_body(delta_text("前半句"), delta_text("后半句"), fail_after=1)]
    provider, bodies, _ = provider_for(settings, steps)
    events = []
    reply = run_stream(provider, request_for(settings), collecting_observer(events))

    assert len(bodies) == 1  # 半截失败不重发、不降级
    assert [event.data["text"] for event in events] == ["前半句"]
    assert reply.text == "前半句"
    assert reply.error == E_LLM_TIMEOUT
    assert reply.finish_reason == "timeout"


def test_loop_stops_a_half_written_stream_without_touching_history(
    settings, registry, session, conn, turn
):
    steps = [stream_body(delta_text("前半句"), delta_text("后半句"), fail_after=1)]
    provider, bodies, _ = provider_for(settings, steps)
    events = []
    result = turn(session, registry, provider, "在吗", stream=True, observer=collecting_observer(events))

    # ① 请求侧走的确实是流式
    assert bodies[0]["stream"] is True
    # ② 行为侧：迭代 → 一个分片 → 收尾，没有第二次调用
    assert [event.kind for event in events] == ["iteration", "text_delta", "done"]
    assert len(bodies) == 1
    # ③ 结果侧：半截回复就是最终回复，并且错误码可查
    assert result.reply == "前半句"
    assert result.error == E_LLM_TIMEOUT
    assert result.finish_reason == "timeout"
    # 增量只进内存：半截回复绝不落 chat_log / 历史（§5.4 落盘一致性）
    assert session.history == []
    assert conn.execute("SELECT COUNT(*) FROM chat_log").fetchone()[0] == 0
