"""价目表与成本计算（TECH-DESIGN §4.4）。

**价格以官网为准**：下表查询日期 2026-09-20，单位「元 / 百万 token」。
价格变动时只改这里，``usage.jsonl`` 里的历史行不会被重算（成本是当时的事实）。

只收「我们真的会路由到」的模型：调用过的模型必须在表里有自己的一行，否则成本会静默
按兜底价计（偏乐观），账就自证不了——这条纪律由 ``evals/deterministic/test_pricing.py`` 钉住。
"""

from __future__ import annotations

from yixiang.runtime.models import Usage

# 查询日期：2026-09-20。三元组 = (缓存未命中输入, 缓存命中输入, 输出)
# deepseek-flash 官网分「高峰 / 空闲」两档（空闲 = 高峰的一半），这里取**高峰**价：宁可高估。
# 只收官网上架的名字：deepseek-chat / deepseek-reasoner 已下架（实测仍能调用，但服务端把
# model 换成 deepseek-flash），它们的行留在表里等于拿一个我们并不在付的价记账。
PRICES: dict[str, tuple[float, float, float]] = {
    "deepseek-flash": (2.0, 0.04, 8.0),  # 当前唯一在用的模型（main / gate / judge / utility）
    "glm-4-flash": (0.0, 0.0, 0.0),  # 免费额度，但仍记账 token
}

# 未知模型的兜底价 = 表里最贵的一档：宁可高估，不掩盖成本。
# 注意这是"表内最贵"，不是"市面最贵"：真要用更贵的模型（比如 deepseek-v4-pro），
# 得先给它补一行，否则会按 deepseek-flash 的价记（偏乐观）。
DEFAULT_PRICE: tuple[float, float, float] = max(PRICES.values())


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
