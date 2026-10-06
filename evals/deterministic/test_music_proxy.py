"""`yixiang.media.proxy`：歌词舞台的同源转发（白名单 + 只转发不解析）。

这一层是**全项目唯一**一处"服务端替页面外呼"的代码，所以用例的重点不是"能不能拿到
词"，而是那道口子上有没有锁：不认识的域名、不是 https、白名单之外的重定向，都必须
在**出网之前**被挡住。

全部走 ``httpx.MockTransport``，一个字节不出网。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from yixiang.media import proxy


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _echo(request: httpx.Request) -> httpx.Response:
    """把收到的请求原样回一份，好用例断言"到底发到哪、带了什么头"。"""

    return httpx.Response(
        200,
        content=request.content,
        headers={"content-type": "application/json; charset=utf-8"},
    )


# --------------------------------------------------------------------- 白名单
def test_the_three_known_hosts_and_kugou_subdomains_are_allowed():
    assert proxy.is_allowed_host("u.y.qq.com")
    assert proxy.is_allowed_host("y.gtimg.cn")
    assert proxy.is_allowed_host("amll-ttml-db.stevexmh.net")
    assert proxy.is_allowed_host("kugou.com")  # 主域本身
    assert proxy.is_allowed_host("lyrics.kugou.com")  # 子域
    assert proxy.is_allowed_host("webfile.kgimg.com")


def test_look_alike_hosts_are_not_allowed():
    """``notkugou.com`` 这种"拿主域做后缀"的域名不能白名单里混进去。"""

    assert not proxy.is_allowed_host("notkugou.com")
    assert not proxy.is_allowed_host("kugou.com.evil.net")
    assert not proxy.is_allowed_host("evil-kgimg.com")
    assert not proxy.is_allowed_host("localhost")


@pytest.mark.parametrize(
    "url",
    [
        "http://u.y.qq.com/cgi-bin/musicu.fcg",  # 不是 https
        "https://127.0.0.1:9999/x",  # 内网地址
        "https://user:pw@u.y.qq.com/x",  # 带账号密码
        "file:///etc/passwd",  # 不是 http 家族
        "/relative",  # 没有域名
        "",
    ],
)
def test_bad_targets_are_refused_before_any_network_call(url: str):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    with pytest.raises(proxy.ProxyError):
        proxy.fetch(url, client=_client(handler))

    assert calls == []  # 关键：一个包都没发出去


def test_a_host_outside_the_whitelist_is_refused():
    with pytest.raises(proxy.ProxyError) as info:
        proxy.fetch("https://example.com/lyrics", client=_client(_echo))

    assert info.value.code == "host_not_allowed"
    assert info.value.status == 400


def test_only_get_and_post_can_be_forwarded():
    with pytest.raises(proxy.ProxyError) as info:
        proxy.fetch("https://y.gtimg.cn/x", method="DELETE", client=_client(_echo))

    assert info.value.code == "bad_method"
    assert info.value.status == 405


# ----------------------------------------------------------------------- 转发
def test_a_get_comes_back_with_the_upstream_bytes_and_type():
    payload = "<tt><p>词</p></tt>".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://y.gtimg.cn/qqmusic/lyric/abc?format=ttml"
        assert request.method == "GET"
        return httpx.Response(200, content=payload, headers={"content-type": "application/ttml+xml"})

    status, body, content_type = proxy.fetch(
        "https://y.gtimg.cn/qqmusic/lyric/abc?format=ttml", client=_client(handler)
    )

    assert status == 200
    assert body == payload
    assert content_type.startswith("application/ttml")


def test_a_post_forwards_the_body_and_adds_what_the_page_cannot_set():
    """页面里的 fetch 设不了 ``User-Agent`` / ``Cookie``，这两个只能在这一层补。"""

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content"] = request.content
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"code": 0})

    payload = b'{"comm":{"ct":11,"cv":"1003006"}}'
    status, body, _ = proxy.fetch(
        "https://u.y.qq.com/cgi-bin/musicu.fcg",
        method="POST",
        body=payload,
        content_type="application/json",
        client=_client(handler),
    )

    assert status == 200
    assert body == b'{"code":0}'  # httpx 编码 JSON 用的是紧凑分隔符，这一层只原样透传
    assert seen["content"] == payload
    assert seen["headers"]["cookie"] == proxy.QQ_COOKIE
    assert seen["headers"]["user-agent"] == proxy.USER_AGENT
    assert seen["headers"]["content-type"] == "application/json"


def test_other_hosts_do_not_get_the_qq_cookie():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return httpx.Response(200)

    proxy.fetch("https://lyrics.kugou.com/search", client=_client(handler))

    assert "cookie" not in seen["headers"]
    assert seen["headers"]["user-agent"] == proxy.USER_AGENT


def test_an_upstream_error_status_is_passed_through():
    """上游回 4xx 就照原样回去：页面自己看 ``response.ok``，别改号。"""

    status, body, _ = proxy.fetch(
        "https://u.y.qq.com/x",
        client=_client(lambda request: httpx.Response(404, content=b"nope")),
    )

    assert (status, body) == (404, b"nope")


def test_a_transport_failure_becomes_a_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("不接")

    with pytest.raises(proxy.ProxyError) as info:
        proxy.fetch("https://u.y.qq.com/x", client=_client(handler))

    assert info.value.code == "upstream"
    assert info.value.status == 502


def test_a_redirect_out_of_the_whitelist_is_refused():
    """白名单里的域名把我们甩到别处：跟过去的那一跳也必须再过一次白名单。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "u.y.qq.com":
            return httpx.Response(302, headers={"location": "https://example.com/steal"})
        return httpx.Response(200, content=b"should not be returned")

    with pytest.raises(proxy.ProxyError) as info:
        proxy.fetch("https://u.y.qq.com/x", client=_client(handler))

    assert info.value.code == "host_not_allowed"


def test_a_redirect_inside_the_whitelist_is_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "u.y.qq.com":
            return httpx.Response(302, headers={"location": "https://c.y.qq.com/ok"})
        return httpx.Response(200, content=b"followed")

    status, body, _ = proxy.fetch("https://u.y.qq.com/x", client=_client(handler))

    assert (status, body) == (200, b"followed")


def test_an_oversized_answer_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (proxy.MAX_BYTES + 1))

    with pytest.raises(proxy.ProxyError) as info:
        proxy.fetch("https://y.gtimg.cn/big", client=_client(handler))

    assert info.value.code == "too_large"
