"""仅 Bangumi 的出口代理（``YIXIANG_BANGUMI_PROXY``）：四条链路都走它，别的链路一字节不碰。

为什么要这么一项配置（``00-context.md`` 的实测表）：本机直连 ``api.bgm.tv:443``
**超时**（8.3s ``ConnectTimeout``，开着 VPN 也一样——VPN 是代理模式、不接管直连），
Windows 系统代理是关的（``ProxyEnable=0``），所以 httpx 自己不会走代理；把
``HTTPS_PROXY`` 指到 ``127.0.0.1:7897`` 之后全部接口 200。

但"设一个全局环境变量"的粒度不对：它让**所有**出站流量改道（模型端点、TMDb、
将来任何一个新接口）。代理在这台机器上只是 **Bangumi 的可达性问题**，所以收成一项
显式配置，只喂给 Bangumi 那几条链路；留空 = 老行为（httpx 照旧看环境变量 / 系统代理）。

纪律：**一条网络请求都不发**。用例把 ``httpx.Client`` 换成只记 kwargs 的探针，
真正发请求的那一层由探针里的 ``MockTransport`` 接管。探针**不吃** ``proxy``——
真把 proxy 交给 httpx 会去连代理，那就出网了。
"""

from __future__ import annotations

from typing import Any

import httpx
from conftest import FIXED_NOW, make_settings

from yixiang.config import Settings
from yixiang.rag import ingest
from yixiang.runtime.models import FixedClock
from yixiang.tools import bangumi
from yixiang.tools import bangumi_collections as bc
from yixiang.tools.registry import Deps, build_registry

PROXY = "http://127.0.0.1:7897"
SUBJECT_ID = 346873
# 探针永远继承**真的** httpx.Client：monkeypatch 会替换 ``httpx.Client`` ，
# 一条用例里替换两次时，第二次若继续写 ``class SpyClient(httpx.Client)``
# 继承到的就是第一个探针，``transport`` 会被传两遍（TypeError）。
_REAL_CLIENT = httpx.Client


class SyncSleep:
    """记录睡眠但不真睡（与 ``test_ingest_fetch.py`` 同款）。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(float(seconds))


def _spy_client(monkeypatch, handler) -> list[dict[str, Any]]:
    """把 ``httpx.Client`` 换成探针：记下每次构造的 kwargs，请求本身走 MockTransport。"""
    seen: list[dict[str, Any]] = []

    class SpyClient(_REAL_CLIENT):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.append(dict(kwargs))
            # 探针不吃 proxy：真交给 httpx 就会建一条代理连接，用例就出网了。
            # 断言的是"代码把哪个值交给了 Client"，不是"代理真的通了"。
            kwargs.pop("proxy", None)
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", SpyClient)
    return seen


def _subject_row() -> dict:
    """``/v0/search/subjects`` 的一行（字段按 2026-09-20 实测取）。"""
    return {
        "id": SUBJECT_ID,
        "name": "サマータイムレンダ",
        "name_cn": "夏日重现",
        "date": "2022-04-15",
        "platform": "TV",
        "rating": {"score": 8.4, "rank": 120},
        "tags": [{"name": "悬疑"}],
    }


def _search_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"total": 1, "data": [_subject_row()]})


def _subject_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_subject_row())


def _collections_handler(request: httpx.Request) -> httpx.Response:
    """收藏链路第一步先问 ``/v0/me``，第二步才是收藏页。"""
    if str(request.url).split("?")[0] == bc.ME_ENDPOINT:
        return httpx.Response(200, json={"id": 1, "username": "yixiang-probe"})
    return httpx.Response(200, json={"data": [], "limit": 100, "offset": 0, "total": 0})


# ------------------------------------------------------------------ 配置本身
def test_the_proxy_setting_defaults_to_empty_and_the_env_var_reaches_settings(tmp_path):
    """默认空 = 老行为（谁都不改道）；写了 env 才进 Settings。"""
    assert Settings().bangumi_proxy == ""

    loaded = Settings.load(
        env_file=None,
        environ={"YIXIANG_BANGUMI_PROXY": PROXY},
        project_root=tmp_path,
    )

    assert loaded.bangumi_proxy == PROXY
    # 它只是一条出口配置：填了不该让 validate() 报错（没有密钥才该报）
    assert "BANGUMI_PROXY" not in "\n".join(loaded.validate())


def test_env_example_ships_the_key(repo_root):
    """示例文件是别人抄配置的唯一来源：漏了它，这项能力在抄的人那里不存在。"""
    text = (repo_root / ".env.example").read_text(encoding="utf-8")

    assert "YIXIANG_BANGUMI_PROXY=" in text


# ------------------------------------------------------------------ live 工具
def test_search_and_subject_hand_the_proxy_to_their_own_client(tmp_path, monkeypatch):
    """两个免 token 的 live 工具都要走它：搜索与条目详情。"""
    seen = _spy_client(monkeypatch, _search_handler)

    out = bangumi.search_bangumi(
        None, FixedClock(FIXED_NOW).now, "夏日重现", proxy=PROXY, sleep=SyncSleep()
    )

    assert "夏日重现" in out
    assert seen == [
        {
            "timeout": bangumi.TIMEOUT_S,
            "headers": {"User-Agent": bangumi.USER_AGENT},
            "proxy": PROXY,
        }
    ]

    seen = _spy_client(monkeypatch, _subject_handler)
    out = bangumi.bangumi_subject(SUBJECT_ID, proxy=PROXY, sleep=SyncSleep())

    assert "夏日重现" in out
    assert seen[0]["proxy"] == PROXY


def test_without_a_proxy_the_client_is_built_exactly_as_before(monkeypatch):
    """留空时**不许**往 Client 里塞 ``proxy=""``：老行为是 httpx 自己看环境变量。"""
    seen = _spy_client(monkeypatch, _search_handler)

    bangumi.search_bangumi(None, FixedClock(FIXED_NOW).now, "夏日重现", sleep=SyncSleep())

    assert seen == [
        {"timeout": bangumi.TIMEOUT_S, "headers": {"User-Agent": bangumi.USER_AGENT}}
    ]


def test_the_registry_hands_the_settings_proxy_to_the_bangumi_tools(
    tmp_path, repo_root, monkeypatch
):
    """装配根也要接上：工具自己支持 proxy，注册时不传照样等于没配。"""
    settings = make_settings(tmp_path, repo_root, bangumi_proxy=PROXY)
    deps = Deps(
        conn=None, clock=FixedClock(FIXED_NOW), data_dir=settings.data_dir, source="cli"
    )
    registry = build_registry(settings, deps)
    seen = _spy_client(monkeypatch, _subject_handler)

    assert "夏日重现" in registry.execute("bangumi_search", {"keyword": "夏日重现"})
    assert seen[-1]["proxy"] == PROXY

    assert "夏日重现" in registry.execute("bangumi_subject", {"subject_id": SUBJECT_ID})
    assert seen[-1]["proxy"] == PROXY


# ------------------------------------------------------------------ 收藏画像
def test_the_taste_profile_reads_the_proxy_from_settings(tmp_path, monkeypatch):
    """收藏链路（Task 28）走的是 ``/v0/me`` + 收藏页，同一条出口，同样要代理。"""
    settings = make_settings(tmp_path, repo_root=tmp_path, bangumi_proxy=PROXY)
    settings.bangumi_token = "tok-123"
    seen = _spy_client(monkeypatch, _collections_handler)

    out = bc.sync_taste_profile(settings, sleep=SyncSleep())

    assert "口味画像" in out
    assert len(seen) == 1 and seen[0]["proxy"] == PROXY


# ------------------------------------------------------------------ 抓取与 ops
def test_ingest_fetch_bangumi_walks_the_proxy_but_tmdb_does_not(tmp_path, monkeypatch):
    """批处理抓取里也只有 Bangumi 这一条走它：TMDb 是另一个出口，别顺手带上。"""
    seen = _spy_client(monkeypatch, _search_handler)

    ingest.fetch_bangumi(data_dir=tmp_path, proxy=PROXY, sleep=SyncSleep())
    assert seen[-1]["proxy"] == PROXY

    ingest.fetch_tmdb(data_dir=tmp_path, api_key="key-not-real", sleep=SyncSleep())
    assert "proxy" not in seen[-1]


def test_ops_collect_passes_the_proxy_only_for_bangumi(tmp_path, repo_root):
    """``yixiang rag ingest --source bangumi`` 的接线：ops 层漏传，前面几层都白搭。"""
    from yixiang.ops import rag_cmd

    class FakeIngest:
        def __init__(self) -> None:
            self.bangumi_kwargs: dict[str, Any] = {}
            self.tmdb_kwargs: dict[str, Any] = {}

        def fetch_bangumi(self, **kwargs: Any) -> list[Any]:
            self.bangumi_kwargs = kwargs
            return []

        def fetch_tmdb(self, **kwargs: Any) -> list[Any]:
            self.tmdb_kwargs = kwargs
            return []

    settings = make_settings(tmp_path, repo_root, bangumi_proxy=PROXY)
    fake = FakeIngest()

    rag_cmd._collect(fake, settings, None, source="bangumi", pages=1)
    rag_cmd._collect(fake, settings, None, source="tmdb", pages=1, api_key="key-not-real")

    assert fake.bangumi_kwargs["proxy"] == PROXY
    assert "proxy" not in fake.tmdb_kwargs
