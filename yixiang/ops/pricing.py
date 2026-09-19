"""价目表与成本计算（TECH-DESIGN §4.4）。

**价格以官网为准**：下表查询日期 2026-09-19，单位「元 / 百万 token」。
价格变动时只改这里，``usage.jsonl`` 里的历史行不会被重算（成本是当时的事实）。
"""

from __future__ import annotations

from yixiang.runtime.models import Usage

# 查询日期：2026-09-19。三元组 = (缓存未命中输入, 缓存命中输入, 输出)
PRICES: dict[str, tuple[float, float, float]] = {
    "deepseek-chat": (2.0, 0.5, 8.0),
    "deepseek-reasoner": (4.0, 1.0, 16.0),
    "glm-4-flash": (0.0, 0.0, 0.0),  # 免费额度，但仍记账 token
}

DEFAULT_PRICE: tuple[float, float, float] = (2.0, 0.5, 8.0)


def price_for(model: str) -> tuple[float, float, float]:
    """未知模型退化到默认价（宁可高估，不掩盖成本）。"""
    return PRICES.get(model, DEFAULT_PRICE)


def cost_cny(model: str, usage: Usage) -> float:
    """按「未命中输入 / 命中输入 / 输出」三段计价，返回元（保留 6 位小数）。"""
    miss_price, hit_price, out_price = price_for(model)
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    fresh = max(usage.input_tokens - cached, 0)
    cost = (fresh * miss_price + cached * hit_price + usage.output_tokens * out_price) / 1e6
    return round(cost, 6)


def price_table_text() -> str:
    lines = ["模型                        未命中输入  命中输入  输出   (元/百万 token)"]
    for model, price in sorted(PRICES.items()):
        lines.append(f"{model:<28}{price[0]:>8.2f}{price[1]:>10.2f}{price[2]:>6.2f}")
    return "\n".join(lines)
