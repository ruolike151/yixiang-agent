"""价目表：真实调用过的模型必须在表里有名有姓（TECH §4.4）。

``PRICES`` 是唯一一份价格事实；这里不硬编码外部价格，只钉四条纪律：
  1. 用量里出现过的模型不许静默回落（回落 = 成本账偏乐观）；
  2. 已经不再使用的模型名不许留在表里（留着 = 用一个我们没在跑的价记账）；
  3. 未知模型仍然回落，且兜底价不低于任何已知档（宁可高估，不掩盖成本）；
  4. ``cost_cny`` 用的是**路由到的那个模型**的价，不是写死的默认价。
"""

from __future__ import annotations

import pytest

from yixiang.ops import pricing
from yixiang.runtime.models import Usage


def test_every_model_we_actually_call_has_its_own_price_row():
    # data/usage.jsonl 实测：真实调用过的是 deepseek-flash
    assert "deepseek-flash" in pricing.PRICES


def test_the_local_qwen_has_a_zero_cost_row():
    """本机 Ollama 的 qwen3.5 真的会被路由到（judge / utility）。

    没有这一行的话，它的 token 会按兜底价（= 表内最贵档）记账——本地推理明明不花钱，
    账上却比云端还贵，``ops cost`` 与每日预算就全是假的。
    """
    assert pricing.PRICES["qwen3.5-9b-uncensored-vision:latest"] == (0.0, 0.0, 0.0)

    usage = Usage(input_tokens=10_000, cached_input_tokens=0, output_tokens=2_000)
    assert pricing.cost_cny("qwen3.5-9b-uncensored-vision:latest", usage) == 0.0


def test_the_retired_models_are_not_quoted_any_more():
    """下架的名字留在表里 = 拿一个我们并不在付的价去记账（reasoner 那行就曾写着 4/1/16）。"""
    for name in ("deepseek-chat", "deepseek-reasoner"):
        assert name not in pricing.PRICES
        assert name not in pricing.price_table_text()


def test_unknown_model_still_falls_back_to_the_default_price():
    assert pricing.price_for("完全没听过的模型") == pricing.DEFAULT_PRICE


def test_default_price_never_underestimates_a_known_model():
    fallback = pricing.price_for("完全没听过的模型")

    for model, price in pricing.PRICES.items():
        # 兜底价是未知模型的价，宁可高估；低过任何已知档就是"不掩盖成本"失守
        assert all(high >= low for high, low in zip(fallback, price, strict=True)), (
            f"未知模型的兜底价比已知档 {model} 还低"
        )


def test_cost_cny_uses_the_routed_models_own_row():
    miss, hit, out = pricing.PRICES["deepseek-flash"]
    usage = Usage(input_tokens=1000, cached_input_tokens=200, output_tokens=500)

    expected = round((800 * miss + 200 * hit + 500 * out) / 1e6, 6)

    assert pricing.cost_cny("deepseek-flash", usage) == pytest.approx(expected)


def test_price_table_text_shows_every_row():
    text = pricing.price_table_text()

    assert "deepseek-flash" in text
    for model in pricing.PRICES:
        assert model in text
