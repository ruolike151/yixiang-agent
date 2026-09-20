"""Web 控制台：本机测试前端（TECH §10 的第三个入口，ADR-3）。

这一组用例把"前端能看到什么、能改什么、改不动的时候说什么"钉死在测试里。分两层，
与 ``yixiang/web/`` 的两层一一对应：

  * ``ConsoleAPI``（``console.py``）——业务适配层，**不起服务**就能断言：人设超限
    是不是一个字节都没写、QQ 开了却没白名单是不是被拒、``read_file`` 能不能读到
    刚上传的文件；
  * ``ConsoleServer``（``server.py``）——HTTP 层，起在 ``127.0.0.1:0``（系统挑端口）
    上用真请求打一遍：状态码、JSON、multipart 上传、SSE 流式对话、静态文件与路径
    穿越。

全程离线：假 Provider 出剧本，一次网络外呼都没有（§13.6）。
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from fake_provider import FakeProvider, text_reply

from yixiang.app import App
from yixiang.config import Settings
from yixiang.memory.core_files import CHAR_LIMITS, MEMORY_MAX_LINES
from yixiang.ops.usage import JsonlUsageSink
from yixiang.web import ConsoleAPI, ConsoleError, build_server
from yixiang.web.console import MAX_UPLOAD_BYTES, mask_secret, render_env
from yixiang.web.server import parse_multipart

REPLY = "排好了：今天先读 RAG 论文。"


# --------------------------------------------------------------------- 夹具
def make_api(settings, clock, *script, env_text: str | None = None) -> ConsoleAPI:
    """一个**不启服务**的控制台：假 Provider + tmp_path 里的 ``.env``。

    ``app_factory`` 而不是先建好 ``App``：``sqlite3`` 的连接归建它的线程所有，HTTP
    层要在自己的工作线程里装配，用例这边只交出"怎么装配"。
    """
    env_file = Path(settings.data_dir).parent / ".env"
    if env_text is not None:
        env_file.write_text(env_text, encoding="utf-8")
    sink = JsonlUsageSink(settings.usage_path, clock=clock)

    def factory(current):
        return App.from_settings(
            current,
            provider=FakeProvider(*script, usage_sink=sink, clock=clock, stream_pieces=4),
            usage_sink=sink,
            clock=clock,
        )

    return ConsoleAPI(settings, app_factory=factory, env_file=env_file)


def multipart_body(
    filename: str, data: bytes, *, field: str = "file", boundary: str = "----yixiangTest"
) -> tuple[bytes, str]:
    """手拼一个浏览器 ``FormData`` 那种 multipart 体（不引第三方库）。"""
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return head + data + tail, f"multipart/form-data; boundary={boundary}"


def parse_sse(raw: bytes) -> list[dict[str, Any]]:
    """把 SSE 响应体读成事件列表（只认 ``data:`` 行，注释行丢掉）。"""
    return [
        json.loads(line[len("data: ") :])
        for line in raw.decode("utf-8").splitlines()
        if line.startswith("data: ")
    ]


class Client:
    """够用的 HTTP 客户端：4xx / 5xx 也把状态码与响应体交回来，不抛异常。"""

    def __init__(self, base: str) -> None:
        self.base = base

    def call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        data: bytes | None = None
        merged = dict(headers or {})
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                data = bytes(body)
            else:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                merged.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=merged
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()


# --------------------------------------------------------------------- 总览
def test_state_shows_models_counters_and_masks_the_key(settings, clock):
    api = make_api(settings, clock)

    state = api.state()

    assert state["counters"]["tools"] == 16
    assert state["counters"]["skills"] == 0  # tmp 里还没有 skills/
    assert state["models"]["main"] == "deepseek-flash"
    assert state["models"]["gate"] == "deepseek-flash"  # 留空 → 回落主模型
    assert state["session"] == {"id": "web:default", "source": "web", "turns": 0}
    assert state["ready"]["api_key"] is True
    assert state["errors"] == []  # 配置齐全：启动自检没有红灯
    assert state["cost"]["period"] == "day"

    # 密钥只出掩码：整个 payload 里都找不到明文
    assert state["provider"]["api_key_mask"] == mask_secret(settings.api_key)
    assert state["provider"]["api_key_set"] is True
    assert settings.api_key not in json.dumps(state, ensure_ascii=False)


def test_state_reports_validation_errors_instead_of_hiding_them(settings, clock):
    settings.api_key = ""
    api = make_api(settings, clock)

    state = api.state()

    assert state["ready"]["api_key"] is False
    assert any("YIXIANG_API_KEY" in item for item in state["errors"])


# --------------------------------------------------------------------- 人设
def test_persona_round_trip_and_limit_keeps_the_file_untouched(settings, clock):
    api = make_api(settings, clock)

    initial = api.persona()
    assert [item["name"] for item in initial["files"]] == ["soul.md", "user.md"]
    assert initial["files"][0]["source"] == "template"  # 实体还没建：如实标注是回落

    saved = api.save_persona("soul.md", "你是以湘。\n\n## Learned rules\n- 先确认再动手。")
    soul = next(item for item in saved["files"] if item["name"] == "soul.md")
    assert saved["ok"] is True
    assert soul["source"] == "data"
    assert "先确认再动手" in soul["text"]
    assert soul["limit"] == CHAR_LIMITS["soul.md"]
    assert (settings.data_dir / "soul.md").is_file()

    # 超限：文件一个字节都不动，错误里带上限与实际值
    before = (settings.data_dir / "soul.md").read_text(encoding="utf-8")
    with pytest.raises(ConsoleError) as exc:
        api.save_persona("soul.md", "长" * (CHAR_LIMITS["soul.md"] + 1))
    assert exc.value.code == "limit_exceeded"
    assert exc.value.payload["limit"] == CHAR_LIMITS["soul.md"]
    assert exc.value.payload["field"] == "soul.md"
    assert (settings.data_dir / "soul.md").read_text(encoding="utf-8") == before

    # 白名单之外的文件名：说清只能改哪两个
    with pytest.raises(ConsoleError) as exc:
        api.save_persona("memory.md", "# 随便")
    assert exc.value.code == "bad_field"
    assert exc.value.payload["allowed"] == ["soul.md", "user.md"]


# --------------------------------------------------------------------- 记忆
def test_memory_round_trip_parses_entries_and_syncs(settings, clock):
    api = make_api(settings, clock)

    initial = api.memory()
    assert initial["path"].endswith("memory.md")
    assert initial["max_lines"] == MEMORY_MAX_LINES

    text = "\n".join(
        [
            "# Memory — yixiang 记得的事",
            "<!-- yixiang:format=v1 -->",
            "",
            "## 用户",
            "- [1] 在做两周 RAG 复习计划",
            "",
            "## 偏好",
        ]
    )
    saved = api.save_memory(text)

    assert saved["ok"] is True
    assert saved["used_lines"] == len(text.splitlines())
    assert [(entry["id"], entry["content"]) for entry in saved["entries"]] == [
        (1, "在做两周 RAG 复习计划")
    ]
    assert saved["problems"] == []

    report = api.sync_memory()
    assert report["summary"]  # 文件为准：同步真的跑过，且给出一句人话
    assert report["warnings"] == []

    # 超行数：与命令行同一条规则（write_core_file 的上限），文件一个字节不动
    before = (settings.data_dir / "memory.md").read_text(encoding="utf-8")
    with pytest.raises(ConsoleError) as exc:
        api.save_memory("\n".join(f"- [n] 第 {index} 条" for index in range(MEMORY_MAX_LINES + 5)))
    assert exc.value.code == "limit_exceeded"
    assert exc.value.payload["unit"] == "行"
    assert (settings.data_dir / "memory.md").read_text(encoding="utf-8") == before


# --------------------------------------------------------------------- 配置
def test_save_config_rewrites_env_in_place_and_blocks_bad_values(settings, clock):
    env_text = (
        "# 我的配置\n"
        "YIXIANG_MAIN_MODEL=deepseek-flash  # 主模型\n"
        "YIXIANG_API_KEY=sk-test-placeholder-key\n"
    )
    api = make_api(settings, clock, env_text=env_text)

    snapshot = api.config()
    assert snapshot["fields"]["api_key"] == ""  # 明文永不出站：要改就填新的
    assert snapshot["secret_mask"]["api_key"] == mask_secret(settings.api_key)
    assert snapshot["env_file_exists"] is True
    assert snapshot["choices"]["model_suggestions"][0] == "deepseek-flash"

    result = api.save_config({"main_model": "deepseek-v4-pro", "api_key": ""})
    assert result["ok"] is True
    assert result["ignored"] == []
    assert result["written"] == ["YIXIANG_MAIN_MODEL"]
    assert result["config"]["fields"]["main_model"] == "deepseek-v4-pro"
    assert result["restart_required"] is False  # data_dir 没动：不用重启

    written = api.env_file.read_text(encoding="utf-8")
    assert "# 我的配置" in written  # 注释原样保留
    assert "YIXIANG_MAIN_MODEL=deepseek-v4-pro  # 主模型" in written  # 原地替换 + 对齐不变
    assert "YIXIANG_API_KEY=sk-test-placeholder-key" in written  # 密钥留空 = 不改
    assert api.settings.main_model == "deepseek-v4-pro"  # 热更新：下一轮就生效

    # 新键追加（原来没有的键落在文末）
    assert api.save_config({"history_turns": 4})["ok"] is True
    assert "YIXIANG_HISTORY_TURNS=4" in api.env_file.read_text(encoding="utf-8")
    assert api.settings.history_turns == 4

    # 非法值：只拦**新引入**的问题，且 .env 一个字节不动
    before = api.env_file.read_text(encoding="utf-8")
    with pytest.raises(ConsoleError) as exc:
        api.save_config({"loop_max_iter": 99})
    assert exc.value.code == "invalid_config"
    assert exc.value.payload["errors"] == ["YIXIANG_LOOP_MAX_ITER 应在 1~20"]
    assert api.env_file.read_text(encoding="utf-8") == before
    assert api.settings.loop_max_iter == 6

    # 不支持工具调用的模型当主模型：控制台这一关也得拦住（Task 2 的守门不只活在 doctor 里）
    with pytest.raises(ConsoleError) as exc:
        api.save_config({"main_model": "deepseek-reasoner"})
    assert exc.value.code == "invalid_config"
    assert "不支持工具调用" in exc.value.payload["errors"][0]
    assert api.env_file.read_text(encoding="utf-8") == before
    assert api.settings.main_model == "deepseek-v4-pro"  # 拦下 = 热配置没被改坏

    # 已下架的名字同样被拦，且原因说得清是哪一类问题（在售口径 ≠ 能力）
    with pytest.raises(ConsoleError) as exc:
        api.save_config({"main_model": "deepseek-chat"})
    assert exc.value.code == "invalid_config"
    assert any("已下架" in item for item in exc.value.payload["errors"])
    assert api.env_file.read_text(encoding="utf-8") == before

    # 白名单外的字段被忽略；全都被忽略 = 没什么可存
    with pytest.raises(ConsoleError) as exc:
        api.save_config({"qq_token_typo": "x", "unknown": "y"})
    assert exc.value.code == "nothing_to_save"
    assert exc.value.payload["ignored"] == ["qq_token_typo", "unknown"]

    # 必填项不能写空（可行动：说清哪个键、为什么不能空）
    with pytest.raises(ConsoleError) as exc:
        api.save_config({"main_model": ""})
    assert exc.value.code == "bad_value"
    assert exc.value.payload["field"] == "YIXIANG_MAIN_MODEL"


def test_render_env_keeps_comments_and_appends_new_keys():
    text = '# 头部\nYIXIANG_MAIN_MODEL=a  # 主模型\nexport YIXIANG_API_BASE="https://x/v1"\n'

    rendered = render_env(text, {"YIXIANG_MAIN_MODEL": "b", "YIXIANG_LOG_LEVEL": "DEBUG"})

    assert "YIXIANG_MAIN_MODEL=b  # 主模型" in rendered  # 换值不动原来的注释与对齐
    assert 'YIXIANG_API_BASE="https://x/v1"' in rendered  # 没命中的行原样留着
    assert rendered.endswith("YIXIANG_LOG_LEVEL=DEBUG\n")


def test_save_config_still_saves_other_keys_when_env_has_a_retired_model(settings, clock):
    """老 ``.env`` 里躺着退役模型名时，改**别的**键仍然要能存。

    试算的基线是"当前 Settings"，所以已经存在的问题只照实回给前端（``errors`` 里看得见），
    不能把每一次保存都拦死——否则用户会被一个自己没在改的字段困住，连修它的机会都没有。
    """
    env_file = Path(settings.data_dir).parent / ".env"
    env_file.write_text("YIXIANG_MAIN_MODEL=deepseek-chat\n", encoding="utf-8")
    live = Settings.load(
        env_file=env_file,
        environ={},
        project_root=settings.project_root,
        api_key=settings.api_key,
        data_dir=settings.data_dir,
    )
    api = make_api(live, clock)

    result = api.save_config({"history_turns": 5})

    assert result["ok"] is True
    assert "YIXIANG_HISTORY_TURNS=5" in env_file.read_text(encoding="utf-8")
    assert any("已下架" in item for item in result["errors"])  # 红灯照实回给前端
    assert api.settings.main_model == "deepseek-chat"  # 但没被顺手改掉


# --------------------------------------------------------------------- QQ
def test_qq_settings_reject_enabled_without_allowlist(settings, clock):
    api = make_api(settings, clock, env_text="# 空配置\n")

    initial = api.qq()
    assert initial["fields"]["qq_enabled"] is False
    assert initial["allowed_count"] == 0
    assert initial["status"] == "已关闭（P2 才接网关）"

    with pytest.raises(ConsoleError) as exc:
        api.save_qq({"qq_enabled": True})
    assert exc.value.code == "invalid_config"
    assert exc.value.payload["errors"] == [
        "YIXIANG_QQ_ENABLED=1 但 YIXIANG_QQ_ALLOWED 为空：出于安全考虑拒绝启动 QQ 网关"
    ]
    assert api.env_file.read_text(encoding="utf-8") == "# 空配置\n"

    saved = api.save_qq(
        {"qq_enabled": True, "qq_allowed": "10001, 10002", "qq_token": "secret-token-1234"}
    )
    assert saved["ok"] is True
    assert saved["qq"]["allowed_list"] == ["10001", "10002"]
    assert saved["qq"]["status"] == "已开启（白名单 2 个 QQ 号）"
    assert saved["qq"]["fields"]["qq_token"] == ""  # 明文不出站
    assert saved["qq"]["secret_mask"]["qq_token"] == mask_secret("secret-token-1234")
    written = api.env_file.read_text(encoding="utf-8")
    assert "YIXIANG_QQ_ENABLED=1" in written
    assert "YIXIANG_QQ_ALLOWED=10001, 10002" in written
    assert api.settings.qq_enabled is True


# --------------------------------------------------------------------- 会话
def test_history_lists_sessions_and_transcript_is_old_to_new(settings, clock):
    api = make_api(settings, clock, text_reply("R1"), text_reply("R2"), text_reply("R3"))

    api.chat("Q1")
    api.chat("Q2")
    assert [turn["user"] for turn in api.transcript()["turns"]] == ["Q1", "Q2"]

    api.new_session("复习")
    assert api.sessions()["current"] == "web:20260919-1000-复习"
    assert api.transcript()["turns"] == []  # 新会话还没有往来

    api.chat("Q3")
    listing = api.sessions()
    assert {item["session_id"] for item in listing["sessions"]} == {
        "web:default",
        "web:20260919-1000-复习",
    }
    titled = next(item for item in listing["sessions"] if item["session_id"] == "web:default")
    assert titled["title"] == "Q1"  # 标题 = 首条用户消息
    assert titled["turns"] == 2

    back = api.transcript("web:default")
    assert (back["switched"], back["session_id"], back["source"]) == (True, "web:default", "web")
    assert [turn["user"] for turn in back["turns"]] == ["Q1", "Q2"]
    assert api.sessions()["current"] == "web:default"
    assert back["turns"][0]["reply"] == "R1"
    assert back["turns"][0]["tools"] == []

    with pytest.raises(ConsoleError) as exc:
        api.switch("  ")  # 空 id：别让前端悄悄切到别的会话
    assert exc.value.code == "bad_session"


def test_chat_streams_events_and_reports_the_turn(settings, clock):
    api = make_api(settings, clock, text_reply(REPLY))

    events: list[dict[str, Any]] = []
    result = api.chat("帮我安排今天", events.append)

    kinds = [event["kind"] for event in events]
    assert kinds[0] == "iteration"  # loop 的第一件事是宣告"这一轮开始了"
    assert "done" in kinds
    assert kinds[-1] == "result"
    assert "".join(e.get("text", "") for e in events if e["kind"] == "text_delta") == REPLY
    assert result["reply"] == REPLY
    assert result["finish_reason"] == "stop"
    assert result["model"] == "deepseek-flash"
    assert result["tools"] == []
    assert set(result["usage"]) == {"in", "cached_in", "out"}

    # 落盘照旧：trace 与成本账各归各的（入口只搬文本，不新长一条链路）
    assert (settings.traces_dir / "2026-09-19.jsonl").is_file()
    assert settings.usage_path.is_file()

    with pytest.raises(ConsoleError) as exc:
        api.chat("   ")
    assert exc.value.code == "empty_message"


def test_chat_without_api_key_says_where_to_configure_it(settings, clock):
    settings.api_key = ""
    api = make_api(settings, clock)

    with pytest.raises(ConsoleError) as exc:
        api.chat("在吗")

    assert exc.value.code == "no_api_key"
    assert "模型配置" in exc.value.message


# --------------------------------------------------------------------- 上传
def test_upload_sanitises_names_and_never_overwrites(settings, clock):
    api = make_api(settings, clock)

    first = api.upload("../../evil.md", "第一份".encode())
    assert first["path"] == "uploads/2026-09-19-evil.md"
    assert first["bytes"] == len("第一份".encode())
    assert (settings.data_dir / "uploads" / "2026-09-19-evil.md").is_file()

    second = api.upload("../../evil.md", b"second")
    assert second["name"] == "2026-09-19-evil-2.md"  # 同名不覆盖：第一份还在
    kept = (settings.data_dir / "uploads" / "2026-09-19-evil.md").read_text(encoding="utf-8")
    assert kept == "第一份"

    with pytest.raises(ConsoleError) as exc:
        api.upload("empty.md", b"")
    assert exc.value.code == "empty_file"

    with pytest.raises(ConsoleError) as exc:
        api.upload("big.md", b"x" * (MAX_UPLOAD_BYTES + 1))
    assert exc.value.status == 413
    assert exc.value.code == "too_large"


def test_uploaded_file_is_readable_and_escapes_are_refused(settings, clock):
    api = make_api(settings, clock)
    relative = api.upload("notes.md", "# 笔记\n- 复习 RAG".encode())["path"]
    registry = api.app.registry

    outcome = registry.run("read_file", {"path": relative})
    assert outcome.ok is True
    assert '<external_content source="file">' in outcome.output  # 外部内容照旧包住
    assert "复习 RAG" in outcome.output

    missing = registry.run("read_file", {"path": "uploads/没有这个.md"})
    assert missing.ok is False
    assert "not_found" in missing.output

    escaped = registry.run("read_file", {"path": "../../secret.md"})
    assert escaped.ok is False
    assert "路径越界" in escaped.output


# --------------------------------------------------------------------- HTTP
def test_http_layer_serves_api_static_upload_and_sse(settings, clock):
    api = make_api(
        settings, clock, text_reply(REPLY), env_text="YIXIANG_MAIN_MODEL=deepseek-flash\n"
    )
    server = build_server(settings, port=0, api=api, quiet=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    client = Client(server.url.rstrip("/"))
    try:
        # 读接口
        status, raw = client.call("GET", "/api/state")
        assert status == 200
        state = json.loads(raw)
        assert state["counters"]["tools"] == 16
        assert state["session"]["id"] == "web:default"
        assert state["provider"]["api_key_mask"] == mask_secret(settings.api_key)
        assert json.loads(client.call("GET", "/api/tools")[1])["count"] == 16
        assert json.loads(client.call("GET", "/api/skills")[1])["count"] == 0

        # 前端外壳与静态资源
        status, raw = client.call("GET", "/")
        assert status == 200
        assert b"<title>" in raw and "控制台".encode() in raw
        assert b":root" in client.call("GET", "/style.css")[1]
        assert b"/api/chat" in client.call("GET", "/app.js")[1]

        # 静态文件只读：写动词与不存在的页面都如实回错
        status, raw = client.call("POST", "/index.html", body={})
        assert status == 405 and json.loads(raw)["error"] == "method_not_allowed"
        status, raw = client.call("GET", "/nope.html")
        assert status == 404 and json.loads(raw)["error"] == "not_found"
        status, raw = client.call("GET", "/%2e%2e/config.py")  # 路径穿越
        assert status == 404 and json.loads(raw)["error"] == "not_found"
        status, raw = client.call("GET", "/api/nope")
        assert status == 404 and json.loads(raw)["error"] == "not_found"

        # 写接口：配置真落盘、非法值如实回 400
        status, raw = client.call("PUT", "/api/config", body={"history_turns": 5})
        assert status == 200, raw
        assert json.loads(raw)["ok"] is True
        assert "YIXIANG_HISTORY_TURNS=5" in api.env_file.read_text(encoding="utf-8")
        status, raw = client.call("PUT", "/api/persona", body={"name": "memory.md", "text": "x"})
        assert status == 400 and json.loads(raw)["error"] == "bad_field"
        status, raw = client.call("PUT", "/api/config", body={"loop_max_iter": 99})
        assert status == 400 and json.loads(raw)["error"] == "invalid_config"

        # multipart 上传 → data/uploads/（随后就能被 read_file 读到）
        body, content_type = multipart_body("笔记.md", "# 笔记\n- 复习 RAG".encode())
        status, raw = client.call(
            "POST", "/api/upload", body=body, headers={"Content-Type": content_type}
        )
        assert status == 200, raw
        uploaded = json.loads(raw)
        assert uploaded["path"].startswith("uploads/")
        assert (settings.data_dir / uploaded["path"]).is_file()

        status, raw = client.call("POST", "/api/upload", body=b"")
        assert status == 400 and json.loads(raw)["error"] == "bad_multipart"

        # 流式对话：text_delta…→ result → end
        status, raw = client.call("POST", "/api/chat", body={"text": "帮我安排今天"})
        assert status == 200
        events = parse_sse(raw)
        kinds = [event["kind"] for event in events]
        assert kinds[0] == "iteration"
        assert kinds[-2:] == ["result", "end"]
        deltas = "".join(e.get("text", "") for e in events if e["kind"] == "text_delta")
        assert deltas == REPLY
        assert events[-2]["reply"] == REPLY

        # 空消息在开流之前就挡掉：前端拿到的是一条干净的 400 JSON
        status, raw = client.call("POST", "/api/chat", body={"text": "  "})
        assert status == 400 and json.loads(raw)["error"] == "empty_message"

        # 历史读得回来（会话 id 里带冒号，要走 URL 编码）
        status, raw = client.call("GET", "/api/session/web%3Adefault")
        assert status == 200
        assert [turn["user"] for turn in json.loads(raw)["turns"]] == ["帮我安排今天"]
    finally:
        server.shutdown()
        server.close_all()
        thread.join(timeout=5)
    assert thread.is_alive() is False


def test_chunked_upload_gets_an_actionable_error_not_a_confusing_one(settings, clock):
    """``Transfer-Encoding: chunked`` 没有 Content-Length。

    服务端只按 Content-Length 读体（本机控制台够用），所以要在这里就说清楚"换个
    客户端"，而不是让 multipart 解析器回一句"没有带文件名的字段"把调用方引偏。
    """
    api = make_api(settings, clock, text_reply(REPLY), env_text="YIXIANG_MAIN_MODEL=x\n")
    server = build_server(settings, port=0, api=api, quiet=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    body, content_type = multipart_body("笔记.md", b"# hi")
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.sendall(
                (
                    "POST /api/upload HTTP/1.1\r\n"
                    f"Host: {host}:{port}\r\n"
                    f"Content-Type: {content_type}\r\n"
                    "Transfer-Encoding: chunked\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                + body
            )
            raw = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                raw += chunk
    finally:
        server.shutdown()
        server.close_all()
        thread.join(timeout=5)

    status_line, _, payload = raw.partition(b"\r\n\r\n")
    assert b" 411 " in status_line.split(b"\r\n")[0]
    body_json = json.loads(payload.decode("utf-8"))
    assert body_json["error"] == "chunked_not_supported"
    assert "Content-Length" in body_json["message"]


def test_parse_multipart_reads_fields_and_filenames():
    body, content_type = multipart_body("笔记.md", b"# hi")

    parts = parse_multipart(content_type, body)

    assert len(parts) == 1
    assert parts[0]["name"] == "file"
    assert parts[0]["filename"] == "笔记.md"
    assert parts[0]["data"] == b"# hi"

    with pytest.raises(ConsoleError) as exc:
        parse_multipart("multipart/form-data", body)
    assert exc.value.code == "bad_multipart"
