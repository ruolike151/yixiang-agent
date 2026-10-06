"""受限的同源转发：歌词舞台要用的那几家在线接口（Folia 构建产物的服务端那一半）。

Folia 在**浏览器**里跑（不是 Electron）时，QQ / 酷狗的接口不是 CORS 友好的，所以它把
请求打成 ``/api/lyric-proxy?url=<编码后的目标>`` 交给我们代发。上游
``spotify-lyrics-stage`` 的 ``localMusicService`` 就是这么写的——这里是它的另一半；
不实现这一半，舞台上那条 ``[QQMusic] Search failed`` 就永远在，QQ 词也就永远来不了。

这一层只做三件事，别的一概不做：

  * **白名单**：``url`` 是页面递进来的，等于把"让服务端替我发请求"开了个口子。只认
    https + 下面那几个域名，且**重定向之后仍然要过白名单**——不认识的域名一律拒。
  * **只转发 GET / POST**：方法、Content-Type、请求体原样带过去，另加桌面端 UA
    （浏览器不允许页面自己设 UA / Cookie，QQ 的接口认这两个）。
  * **回上游的字节、类型、状态码**：不解析、不改写。歌词是 TTML / JSON，怎么解读是
    舞台自己的事，这一层不认任何一个平台的字段名。

外呼走注入的 ``httpx.Client``，所以用例可以完全离线地钉住"发给谁、回了什么"。
"""

from __future__ import annotations

import urllib.parse

import httpx

# 白名单一：整域名（QQ 的三个接口域 + AMLL 的 TTML 词库）。
ALLOWED_HOSTS = frozenset(
    {
        "u.y.qq.com",  # musicu.fcg：QQ 搜歌 / 取词
        "c.y.qq.com",  # 老式 lyric_download 那套
        "y.gtimg.cn",  # 封面 / TTML 词
        "amll-ttml-db.stevexmh.net",  # AMLL 逐字词库
    }
)

# 白名单二：主域 + 任意子域。酷狗的接口与图片散在一堆 CDN 子域上，逐个列不现实。
ALLOWED_DOMAINS = ("kugou.com", "kgimg.com")

# 页面递进来的方法：多一个都不认（别让人拿这个口子发 DELETE）。
PROXY_METHODS = ("GET", "POST")

# QQ 的接口在桌面端就是这么被调的（上游 Electron 分支的同一对头）。
USER_AGENT = "okhttp/3.14.9"
QQ_COOKIE = "tmeLoginType=-1;"

TIMEOUT = 8.0
# 回给页面的字节上限：词与 JSON 都在这之内，超了说明目标不是我们要的东西。
MAX_BYTES = 4 * 1024 * 1024


class ProxyError(Exception):
    """一次转发没做成。``status`` 是**回给页面**的状态码，与上游的状态码无关。"""

    def __init__(self, message: str, *, status: int = 400, code: str = "bad_target") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _client_for(client: httpx.Client | None) -> httpx.Client:
    """不注入就现建一个：与三家 provider 一样，生产代码不显式传 client。"""

    return client if client is not None else httpx.Client(timeout=TIMEOUT)


def target_host(url: str) -> str:
    """``url`` 的域名（小写）；解析不出来 / 不是 https / 带账号密码 → 抛 ``ProxyError``。"""

    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ProxyError(f"这个地址解析不了：{exc}", code="bad_target") from exc
    if parsed.scheme != "https":
        raise ProxyError("只转发 https 地址。", code="bad_target")
    if parsed.username or parsed.password:
        raise ProxyError("地址里不要带账号密码。", code="bad_target")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ProxyError("地址里没有域名。", code="bad_target")
    return host


def is_allowed_host(host: str) -> bool:
    """精确命中白名单一，或者是以白名单二里某个主域结尾的子域。"""

    if host in ALLOWED_HOSTS:
        return True
    return any(host == base or host.endswith("." + base) for base in ALLOWED_DOMAINS)


def check_allowed(url: str) -> str:
    """过白名单就回域名，否则抛 ``ProxyError``。"""

    host = target_host(url)
    if not is_allowed_host(host):
        raise ProxyError(f"这个域名不在白名单里：{host}", code="host_not_allowed")
    return host


def _headers_for(host: str, content_type: str) -> dict[str, str]:
    """转发时自己补的那几个头；页面给不出来的（UA / Cookie）只有这里能补。"""

    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if host == "u.y.qq.com":
        headers["Cookie"] = QQ_COOKIE
    if content_type:
        # 认得出是什么就原样说；认不出就不猜——上游按默认解析。
        headers["Content-Type"] = content_type
    return headers


def fetch(
    url: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    content_type: str = "",
    client: httpx.Client | None = None,
) -> tuple[int, bytes, str]:
    """代发一次请求：``(状态码, 字节, Content-Type)``。

    上游回了 4xx / 5xx 也原样回去——页面自己会看 ``response.ok``，把上游的错号改成
    我们的错号只会让"到底是哪边的问题"更难查。
    """

    if method not in PROXY_METHODS:
        raise ProxyError(f"只转发 {'/'.join(PROXY_METHODS)}。", status=405, code="bad_method")
    host = check_allowed(url)

    try:
        response = _client_for(client).request(
            method,
            url,
            content=body or None,
            headers=_headers_for(host, content_type),
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise ProxyError(f"转发失败：{exc}", status=502, code="upstream") from exc

    # 跟随重定向之后还要再过一次白名单：不能被一个"允许的域名"把我们甩到别处去。
    final_host = (response.url.host or "").lower()
    if not is_allowed_host(final_host):
        raise ProxyError(
            f"转发被重定向到了白名单之外：{final_host}", status=502, code="host_not_allowed"
        )

    payload = response.content
    if len(payload) > MAX_BYTES:
        raise ProxyError(
            f"上游回了 {len(payload)} 字节，超过上限 {MAX_BYTES} 字节。",
            status=502,
            code="too_large",
        )
    return response.status_code, payload, response.headers.get("content-type", "")
