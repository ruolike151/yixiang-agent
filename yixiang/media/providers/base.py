"""在线音乐 provider 的公共契约（Task 7）。

三个平台（网易云 / 酷狗 / QQ）的自建 API 形状完全不同，面板却只认四个动词：
搜歌、取详情、取播放直链、取词。适配器负责把各家 JSON 收成 ``Song`` /
``ProviderLyrics``，上层（``service.py`` / ``console.py``）不认任何一家的字段名。

**这一层只做"翻译"，不做"播放"**：音频直链直接交给浏览器的 ``<audio>``，
音频字节永远不进 yixiang 的 HTTP 层，更不进模型上下文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from yixiang.media.lyrics import LyricLine

# 三家的名字；`Settings` 里的 base 字段名与它一一对应（`{name}_api_base`）。
# **QQ 排第一**——用户点名它优先，面板默认选中项也是它。
PROVIDER_NAMES = ("qq", "netease", "kugou")

# provider 的错误码，与参考项目的 `ProviderErrorCode` 对齐（本计划只用到这些）。
ERROR_CODES = (
    "auth-required",
    "unsupported",
    "unavailable",
    "not-playable",
    "network",
    "invalid-response",
)

DEFAULT_TIMEOUT = 8.0


class ProviderError(Exception):
    """一次 provider 调用失败；``code`` 取 :data:`ERROR_CODES` 之一。

    ``status`` 只在"上游回了非 2xx"时有值——HTTP 层据此区分"要登录（401）"与
    "上游挂了（502）"，见 `console._provider_status()`。
    """

    def __init__(
        self, code: str, message: str, provider: str = "", *, status: int = 0
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"未知的 provider 错误码：{code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider = provider
        self.status = status


@dataclass(frozen=True)
class Song:
    """一首在线歌。``id`` 是**平台自己的** id（网易云数字 / 酷狗 hash / QQ songmid）。

    ``extra`` 放平台私有、回程才用得上的东西（酷狗的 album_id / album_audio_id、
    QQ 的数字 songid）。它**不出现在** ``to_json()`` 里：前端拿它没用。
    """

    provider: str
    id: str
    title: str
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    artwork_url: str = ""
    playable: bool = True
    extra: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "durationMs": self.duration_ms,
            "artworkUrl": self.artwork_url,
            "playable": self.playable,
        }


@dataclass(frozen=True)
class ProviderPage:
    items: tuple[Song, ...] = ()
    has_more: bool = False
    next_offset: int = 0
    total: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "items": [item.to_json() for item in self.items],
            "hasMore": self.has_more,
            "nextOffset": self.next_offset,
            "total": self.total,
        }


@dataclass(frozen=True)
class ProviderLyrics:
    """一家平台给的词。``lines`` 是**逐句**（与本地 / LRCLib 同一个形状）。

    ``karaoke_lyric`` 是**逐字**通道：只有 QQ 填它（QRC 解密后的原始明文，
    形如 ``[start,duration]()word(start,duration)…``）。它原样透传给歌词舞台的
    Folia 引擎（Task 10），不进 ``lines``、不在 provider 层解析。
    """

    lines: tuple[LyricLine, ...] = ()
    word_by_word: bool = False
    is_pure_music: bool = False
    note: str = ""
    karaoke_lyric: str = ""


@runtime_checkable
class OnlineProvider(Protocol):
    """三家的统一门面。registry / console 只按这个协议调用。"""

    name: str
    display_name: str

    def is_available(self) -> bool: ...
    def search_songs(
        self, keyword: str, *, limit: int = 20, offset: int = 0
    ) -> ProviderPage: ...
    def get_song_detail(self, song_id: str) -> Song: ...
    def get_audio_source(self, song: Song) -> str: ...
    def get_lyrics(self, song: Song) -> ProviderLyrics | None: ...


def as_dict(value: Any) -> dict[str, Any]:
    """各家"这个东西可能是对象也可能是 null"太多了，统一收口。"""

    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def pick(data: Any, *keys: str) -> Any:
    """按顺序取第一个"有内容"的键——同一份 JSON 里各家字段别名太多。"""

    node = as_dict(data)
    for key in keys:
        value = node.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def join_artists(items: Any, *name_keys: str) -> str:
    """把 `[{name:…}]` / `[{base:{singer_name:…}}]` / `"甲、乙"` 收成一个字符串。"""

    if isinstance(items, str):
        return items.strip()
    names: list[str] = []
    for entry in as_list(items):
        if isinstance(entry, str):
            name = entry
        else:
            node = as_dict(entry)
            name = pick(node, *name_keys) or pick(as_dict(node.get("base")), *name_keys)
        if name:
            names.append(str(name).strip())
    return ", ".join(names)


class HttpProvider:
    """三家适配器的公共底座：base 归一化 + httpx + 错误映射。

    ``client`` 可注入（用例用 ``httpx.MockTransport``）；不注入就懒建一个常驻客户端。
    未配置 base **不是构造错误**——``is_available()`` 回 False，真调用时才报
    ``unavailable``（见任务文档取舍 2）。
    """

    name = ""
    display_name = ""

    def __init__(self, base: str = "", *, client: httpx.Client | None = None) -> None:
        self.base = (base or "").strip().rstrip("/")
        self._client = client

    def is_available(self) -> bool:
        return bool(self.base)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=DEFAULT_TIMEOUT)
        return self._client

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._fetch("GET", f"{self._require_base()}{path}", params=params)

    def post_json(
        self, url: str, payload: Any, *, headers: dict[str, str] | None = None
    ) -> Any:
        return self._fetch("POST", url, json=payload, headers=headers)

    def get_lyrics(self, song: Song) -> ProviderLyrics | None:
        """默认没词。有歌词能力的适配器自己覆盖（三家都覆盖了：QQ 走 QRC 逐字）。"""

        return None

    # ---- 内部
    def _require_base(self) -> str:
        if not self.base:
            raise ProviderError(
                "unavailable", f"{self.display_name}没配 API 地址。", self.name
            )
        return self.base

    def _fetch(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            response = self.client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderError(
                "network", f"{self.display_name}请求失败：{exc}", self.name
            ) from exc
        if response.status_code == 401:
            # 只有 QQ 会走到这里（它的会话是 24h TTL 的 token），但映射是通用的：
            # "要登录"和"上游挂了"在面板上是两种完全不同的提示。
            raise ProviderError(
                "auth-required", f"{self.display_name}要登录。", self.name, status=401
            )
        if response.status_code >= 400:
            raise ProviderError(
                "network",
                f"{self.display_name}回了 HTTP {response.status_code}。",
                self.name,
                status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(
                "invalid-response", f"{self.display_name}回的不是 JSON。", self.name
            ) from exc

    def require(self, value: Any, what: str) -> Any:
        """适配器里"这个字段本该有"的断言；缺了就归到 invalid-response。"""

        if value in (None, "", [], {}):
            raise ProviderError(
                "invalid-response", f"{self.display_name}没给{what}。", self.name
            )
        return value
