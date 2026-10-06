"""在线音乐 provider 层（Task 7）：三家适配器 + 注册表。

上层只需要 ``from yixiang.media.providers import build_providers, get_provider``，
以及 ``ProviderError`` / ``Song`` / ``ProviderPage`` / ``ProviderLyrics`` 这几个形状。
"""

from yixiang.media.providers.base import (
    ERROR_CODES,
    PROVIDER_NAMES,
    HttpProvider,
    OnlineProvider,
    ProviderError,
    ProviderLyrics,
    ProviderPage,
    Song,
)
from yixiang.media.providers.kugou import KugouProvider
from yixiang.media.providers.netease import NeteaseProvider
from yixiang.media.providers.qq import QqProvider
from yixiang.media.providers.registry import (
    PROVIDER_ORDER,
    availability,
    build_providers,
    get_provider,
)

__all__ = [
    "ERROR_CODES",
    "PROVIDER_NAMES",
    "PROVIDER_ORDER",
    "HttpProvider",
    "KugouProvider",
    "NeteaseProvider",
    "OnlineProvider",
    "ProviderError",
    "ProviderLyrics",
    "ProviderPage",
    "QqProvider",
    "Song",
    "availability",
    "build_providers",
    "get_provider",
]
