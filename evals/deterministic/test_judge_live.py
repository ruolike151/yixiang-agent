"""judge 的 live 路径（``run_judge.py --live``）：跨用例复用同一个 provider。

任务 9 的交付物之一就是"真换一家重跑历史分数"，而这条路第一次真跑就炸在
``RuntimeError: Event loop is closed``：``_complete`` 每个用例一次 ``asyncio.run``，
provider 缓存的 httpx 连接池绑在**那条已经关掉的 loop** 上，于是第二条用例起全灭——
而第一条已经真实计费、报告里却是一张 0 分表。仓库里 ``runtime/eventloop.py`` 开头
写的就是这条规矩（CLI / Web 都走常驻 loop），只有这个脚本漏了。

这里用真 provider + 假传输把它钉死，顺手证明 task 9 的两件事在**这条真实路径**上成立：
生成打 main 家、裁判打 judge 家，且裁判那一发带 ``reasoning_effort="none"``。
全程离线：一次真模型都不连、一分钱都不花（§13.6）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fake_provider import completion_body

from yixiang.ops.release_gate import _load_judge
from yixiang.providers import OpenAICompatibleProvider
from yixiang.runtime import eventloop

MAIN_BASE = "https://api.deepseek.com/v1"
JUDGE_BASE = "http://127.0.0.1:11434/v1"
LOCAL_MODEL = "qwen3.5-9b-uncensored-vision:latest"

# 裁判那几发必须回一个能被 parse_score 吃下的 JSON（否则用例在测解析而不是在测循环）
VERDICT = '{"score": 4, "reasons": ["切题", "语气合适"]}'


def _wire(settings) -> tuple[OpenAICompatibleProvider, list[dict]]:
    """把两家端点都换成假传输，记下每一发的 (url, 模型, 关没关思考)。"""
    settings.api_base = MAIN_BASE
    settings.api_key = "sk-main-not-real"
    settings.judge_api_base = JUDGE_BASE
    settings.judge_api_key = ""
    settings.judge_model = LOCAL_MODEL
    settings.no_think_models = "qwen3.5-*"

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        seen.append(
            {
                "url": str(request.url),
                "model": payload["model"],
                "reasoning_effort": payload.get("reasoning_effort"),
                "auth": request.headers.get("authorization", ""),
                # 这一发跑在哪条 loop 上——见下面的用例，这是本文件真正的钉子
                "loop": id(asyncio.get_running_loop()),
            }
        )
        generator = payload["model"] != LOCAL_MODEL  # 单数次序：奇数发是生成、偶数发是裁判
        text = "好呀，我记下了。" if generator else VERDICT
        return httpx.Response(200, json=completion_body(text))

    provider = OpenAICompatibleProvider(
        settings, transport=httpx.MockTransport(handler), jitter=0.0
    )
    return provider, seen


def test_live_scoring_survives_more_than_one_case(settings, repo_root: Path):
    """两条用例连着跑：这是最小复现（第一条好好的，第二条才是现场）。"""
    module = _load_judge(repo_root)
    cases = module.load_cases(module.DEFAULT_CASES)[:2]
    assert len(cases) == 2

    provider, seen = _wire(settings)
    try:
        results = [module.judge_live(case, provider) for case in cases]
    finally:
        eventloop.run(provider.aclose())
        eventloop.shutdown()

    # ① 结果侧：两条都真判出了分，不是 0 分 + "Event loop is closed"
    assert [item.score for item in results] == [4, 4]
    assert [item.reasons for item in results] == [["切题", "语气合适"]] * 2
    # ② 请求侧：每条用例两次调用，一共四发
    assert len(seen) == 4
    # ③ 一整批只许有一条 loop：httpx 的连接池绑在"建它的那条 loop"上，换 loop 就是
    #    ``Event loop is closed``。这条不变量才是上面那个现场的病根——假传输本身不会
    #    真的建连接池，所以只有把 loop 身份钉住，用例才能在离线状态下复现它。
    assert len({item["loop"] for item in seen}) == 1


def test_live_scoring_sends_each_call_to_its_own_vendor(settings, repo_root: Path):
    """生成 → main 家；裁判 → judge 家（且关掉思考）。这是 task 9 的验收面。"""
    module = _load_judge(repo_root)
    case = module.load_cases(module.DEFAULT_CASES)[0]

    provider, seen = _wire(settings)
    try:
        result = module.judge_live(case, provider)
    finally:
        eventloop.run(provider.aclose())
        eventloop.shutdown()

    assert result.score == 4
    generate, verdict = seen
    assert generate["url"].startswith(MAIN_BASE)
    assert generate["model"] == settings.main_model
    assert generate["auth"] == "Bearer sk-main-not-real"
    assert generate["reasoning_effort"] is None

    assert verdict["url"].startswith(JUDGE_BASE)
    assert verdict["model"] == LOCAL_MODEL
    # 本机端点：不发 Authorization（也不许把 main 的密钥带过去）
    assert verdict["auth"] == ""
    assert verdict["reasoning_effort"] == "none"
