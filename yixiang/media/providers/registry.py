"""三家的注册表：``Settings`` → ``{name: provider}``，以及"按名字取一家"。"""

from __future__ import annotations

import httpx

from yixiang.config import Settings
from yixiang.media.providers.base import OnlineProvider, ProviderError
from yixiang.media.providers.kugou import KugouProvider
from yixiang.media.providers.netease import NeteaseProvider
from yixiang.media.providers.qq import QqProvider

# 面板上三行的顺序（也是用例钉死的顺序）：QQ 第一。
PROVIDER_ORDER = ("qq", "netease", "kugou")


def build_providers(
    settings: Settings, *, client: httpx.Client | None = None
) -> dict[str, OnlineProvider]:
    """建三家适配器；``client`` 注进去给三家**共用**（用例的假后端就是这么塞的）。

    返回普通 ``dict``（Python 3.7+ 保插入顺序），于是面板能照 ``PROVIDER_ORDER`` 画三行。
    """

    return {
        "qq": QqProvider(settings.qq_api_base, client=client),
        "netease": NeteaseProvider(settings.netease_api_base, client=client),
        "kugou": KugouProvider(settings.kugou_api_base, client=client),
    }


def get_provider(providers: dict[str, OnlineProvider], name: str) -> OnlineProvider:
    """按名字取一家。名字不认识 → ``unsupported``；认识但没配 base → ``unavailable``。"""

    provider = providers.get(name)
    if provider is None:
        raise ProviderError("unsupported", f"不认识的平台：{name or '（空）'}。", name)
    if not provider.is_available():
        raise ProviderError(
            "unavailable", f"{provider.display_name}没配 API 地址。", name
        )
    return provider


def availability(providers: dict[str, OnlineProvider]) -> dict[str, bool]:
    """每家"配没配"（面板画灰行用）。"""

    return {name: provider.is_available() for name, provider in providers.items()}
