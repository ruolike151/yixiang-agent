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

import asyncio
import json
import re
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from conftest import PNG_1X1
from fake_provider import FakeProvider, text_reply, usage

from yixiang.app import App
from yixiang.config import Settings
from yixiang.memory.core_files import CHAR_LIMITS, MEMORY_MAX_LINES
from yixiang.ops.usage import JsonlUsageSink
from yixiang.runtime.media import MAX_INLINE_IMAGE_BYTES
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


def content_type_of(client: Client, path: str) -> str:
    """读一个静态资源真正的 Content-Type（Client.call 只回状态码和体，看不到头）。"""
    with urllib.request.urlopen(client.base + path, timeout=10) as response:
        return response.headers.get_content_type()


# --------------------------------------------------------------------- 总览
def test_state_shows_models_counters_and_masks_the_key(settings, clock):
    api = make_api(settings, clock)

    state = api.state()

    assert state["counters"]["tools"] == 22
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
    assert initial["status"] == "已关闭（YIXIANG_QQ_ENABLED 未开）"

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


def test_session_rename_search_export_delete(settings, clock):
    """历史面板的四个动作：改名 / 搜索 / 导出 / 删除（记忆与往来分开）。"""
    api = make_api(settings, clock, text_reply("R1"), text_reply("R2"))

    api.chat("悬疑小说的线索怎么铺")
    api.new_session("复习")
    api.chat("RAG 的召回率怎么算")

    renamed = api.rename_session("web:default", "悬疑笔记")
    titled = next(s for s in renamed["sessions"] if s["session_id"] == "web:default")
    assert titled["title"] == "悬疑笔记"

    # 空标题 = 恢复默认（标题会直接进 DOM，顺便把换行也压平）
    restored = api.rename_session("web:default", "  ")
    back = next(s for s in restored["sessions"] if s["session_id"] == "web:default")
    assert back["title"] == "悬疑小说的线索怎么铺"

    # 搜索按正文命中，返回的形状与 /api/sessions 一致（前端好复用）
    found = api.search_sessions("召回率")
    assert found["query"] == "召回率"
    assert [s["session_id"] for s in found["sessions"]] == ["web:20260919-1000-复习"]
    assert found["current"] == "web:20260919-1000-复习"

    exported = api.export_session("web:default")
    assert [turn["user"] for turn in exported["turns"]] == ["悬疑小说的线索怎么铺"]
    assert exported["turns"][0]["reply"] == "R1"

    with pytest.raises(ConsoleError) as exc:
        api.export_session("web:空的")
    assert (exc.value.code, exc.value.status) == ("empty_session", 404)

    # 正在用的会话不许删
    with pytest.raises(ConsoleError) as exc:
        api.delete_session("web:20260919-1000-复习")
    assert exc.value.code == "session_in_use"

    api.switch("web:default")
    removed = api.delete_session("web:20260919-1000-复习")
    assert removed["removed"] == 1
    assert [s["session_id"] for s in removed["sessions"]] == ["web:default"]

    # 长期记忆不动：删的是往来，不是记忆
    store = api.app.session.store
    store.execute(
        "INSERT INTO facts(subject, content, source, created_at, updated_at)"
        " VALUES ('偏好', '喜欢悬疑', 'user', '2026-09-19T10:00:00+08:00',"
        " '2026-09-19T10:00:00+08:00')"
    )
    store.commit()
    api.switch("web:default")
    assert api.delete_session("web:20260919-1000-复习")["removed"] == 0
    assert store.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1


def test_web_new_session_stays_in_the_web_namespace(settings, clock):
    """Web 的「新会话」永远落在 ``web:``——哪怕当前正站在 QQ 会话里。

    站在 QQ 会话里点新会话、沿用原 source，会造出 ``qq:20260919-1000-复习`` 这种
    "看着像 QQ、其实是网页聊的"会话：QQ 的往来被拆成好几条，``chat_log.source``
    也跟着说假话。所以判据有三条——新 id 是 ``web:``、当前来源是 ``web``、
    原来那条 QQ 会话一轮不少地留在列表里。
    """
    api = make_api(settings, clock, text_reply("R1"), text_reply("R2"))
    qq = api.app.new_session_manager("qq:1904625008", source="qq")
    qq.add_exchange("挪到本周", "好，三项都挪到本周了。")

    api.switch("qq:1904625008")
    listing = api.sessions()
    assert listing["current"] == "qq:1904625008"
    # 列表行带上来源：网页上分得清哪条是 QQ 来的、哪条是自己聊的
    assert {item["session_id"]: item["source"] for item in listing["sessions"]} == {
        "qq:1904625008": "qq"
    }

    created = api.new_session("复习")
    assert created["current"] == "web:20260919-1000-复习"
    assert api.app.session.source == "web"

    api.chat("新会话里的一句")  # 零轮的新会话不进历史列表，聊一句才有得比
    rows = {item["session_id"]: item for item in api.sessions()["sessions"]}
    assert set(rows) == {"qq:1904625008", "web:20260919-1000-复习"}
    assert (rows["qq:1904625008"]["turns"], rows["qq:1904625008"]["source"]) == (1, "qq")
    assert (rows["web:20260919-1000-复习"]["turns"], rows["web:20260919-1000-复习"]["source"]) == (
        1,
        "web",
    )


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


def test_upload_cap_moved_past_the_old_two_megabytes(settings, clock):
    """2MB 是上一版的上限。3MB 的文件现在必须真的落盘（不是"错误文案改小了"）。"""
    api = make_api(settings, clock)

    result = api.upload("论文.pdf", b"x" * 3_000_000)

    assert result["bytes"] == 3_000_000
    assert (settings.data_dir / result["path"]).stat().st_size == 3_000_000
    assert MAX_UPLOAD_BYTES == 30_000_000


def test_uploads_are_listed_from_disk_so_a_reload_still_shows_them(settings, clock):
    """列表必须来自 ``data/uploads/`` 本身。

    只列"本次上传"是删不掉文件的那半个根因：刷新页面后 store 空了，磁盘上的
    文件还在，界面上连行都没有，自然无从删起。
    """
    api = make_api(settings, clock)
    fresh = api.upload("笔记.md", "第一份".encode())
    # 上一版控制台留下的文件（不是这一次传的）：也必须出现在列表里
    leftover = settings.data_dir / "uploads" / "2026-09-01-上次留下的.md"
    leftover.write_text("上一版留下的", encoding="utf-8")

    listing = api.list_uploads()

    assert [item["name"] for item in listing["items"]] == [
        "2026-09-19-笔记.md",
        "2026-09-01-上次留下的.md",
    ]
    assert listing["items"][0]["path"] == fresh["path"]
    assert listing["limit"] == MAX_UPLOAD_BYTES
    # 两条上限不是一回事：能传 30MB，但只有 8MB 以内模型才真的看到像素
    assert listing["inline_limit"] == MAX_INLINE_IMAGE_BYTES
    assert listing["dir"] == str(settings.data_dir / "uploads")


def test_a_single_upload_can_be_deleted_and_the_rest_survive(settings, clock):
    api = make_api(settings, clock)
    first = api.upload("a.md", b"A")
    second = api.upload("b.md", b"B")

    result = api.delete_upload(second["name"])

    assert result["deleted"] == second["name"]
    assert [item["name"] for item in result["items"]] == [first["name"]]
    assert not (settings.data_dir / second["path"]).exists()
    assert (settings.data_dir / first["path"]).is_file()

    with pytest.raises(ConsoleError) as exc:
        api.delete_upload("没有这个.md")
    assert exc.value.status == 404
    assert exc.value.code == "upload_not_found"


def test_deleting_and_previewing_never_escape_the_uploads_dir(settings, clock):
    api = make_api(settings, clock)
    settings.data_dir.mkdir(parents=True, exist_ok=True)  # 还没传过东西时 data/ 可能不存在
    outside = settings.data_dir / "soul.md"
    outside.write_text("不该被删", encoding="utf-8")

    with pytest.raises(ConsoleError) as exc:
        api.delete_upload("../soul.md")
    assert exc.value.status == 404
    assert outside.read_text(encoding="utf-8") == "不该被删"

    with pytest.raises(ConsoleError) as exc:
        api.read_upload("../soul.md")
    assert exc.value.status == 404
    assert outside.read_text(encoding="utf-8") == "不该被删"


def test_clear_uploads_empties_the_workspace(settings, clock):
    api = make_api(settings, clock)
    api.upload("a.md", b"A")
    api.upload("b.md", b"B")

    result = api.clear_uploads()

    assert result["deleted"] == 2
    assert result["items"] == []
    assert list((settings.data_dir / "uploads").iterdir()) == []
    assert api.clear_uploads()["deleted"] == 0  # 本来就空：0 是结果，不是错误


def test_an_image_upload_is_labelled_with_a_preview_url_and_no_read_file_hint(
    settings, clock
):
    """图片和文本走两条路：``read_file`` 读不出图（二进制），图片是"随消息发出去"。"""
    api = make_api(settings, clock)

    result = api.upload("截图.png", PNG_1X1)

    assert result["kind"] == "image"
    assert result["mime"] == "image/png"
    assert result["url"] == f"/api/uploads/{result['name']}/raw"
    assert result["hint"] == ""  # 图片不该往输入框里填 read_file（它读不了图）

    data, mime = api.read_upload(result["name"])
    assert data == PNG_1X1
    assert mime == "image/png"


def test_a_text_upload_is_still_a_plain_file_with_the_read_file_hint(settings, clock):
    api = make_api(settings, clock)

    result = api.upload("笔记.md", "# 笔记".encode())

    assert result["kind"] == "file"
    assert result["mime"] == ""
    assert result["hint"] == f'让我读它：read_file(path="{result["path"]}")'


def test_chat_attaches_an_uploaded_image_to_this_turn_only(settings, clock):
    """图片挂在本轮 user 消息上；历史里只留一行"附图："，绝不每轮重发一遍。"""
    api = make_api(settings, clock, text_reply("看到图了"), text_reply("又一轮"))
    image = api.upload("截图.png", PNG_1X1)

    api.chat("看看这张图", images=[image["path"]])
    first = api.app.provider.requests[-1].messages[-1]
    assert first.role == "user"
    assert "看看这张图" in first.content
    assert first.images == [image["path"]]
    assert image["path"] in first.content  # 附图这一行也进文本：刷新后仍看得见

    api.chat("那第二张呢")
    second = api.app.provider.requests[-1].messages
    assert second[-1].content == "那第二张呢"
    assert second[-1].images is None
    history_users = [message for message in second if message.role == "user"]
    assert history_users[0].images is None  # 上一轮的图不随历史重发
    assert image["path"] in history_users[0].content


def test_chat_allows_an_image_only_turn_but_still_refuses_a_truly_empty_one(
    settings, clock
):
    api = make_api(settings, clock, text_reply("看到了"))

    with pytest.raises(ConsoleError) as exc:
        api.chat("   ")
    assert exc.value.code == "empty_message"

    image = api.upload("截图.png", PNG_1X1)
    api.chat("", images=[image["path"]])
    user = api.app.provider.requests[-1].messages[-1]
    assert user.images == [image["path"]]
    assert image["path"] in user.content  # 一个字都没写时，文本位只剩附图那一行


def test_chat_refuses_an_image_that_is_not_in_the_workspace(settings, clock):
    """发一张不存在的图要说清楚：静默丢掉的话，模型"看图"的结论无从核对。"""
    api = make_api(settings, clock)

    with pytest.raises(ConsoleError) as exc:
        api.chat("看看这张", images=["uploads/没有这张.png"])
    assert exc.value.status == 400
    assert exc.value.code == "bad_image"
    assert "没有这张.png" in exc.value.message

    with pytest.raises(ConsoleError) as exc:
        api.chat("看看这张", images=["../soul.md"])
    assert exc.value.code == "bad_image"


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
        assert state["counters"]["tools"] == 22
        assert state["session"]["id"] == "web:default"
        assert state["provider"]["api_key_mask"] == mask_secret(settings.api_key)
        assert json.loads(client.call("GET", "/api/tools")[1])["count"] == 22
        assert json.loads(client.call("GET", "/api/skills")[1])["count"] == 0

        # 前端外壳与静态资源
        status, raw = client.call("GET", "/")
        assert status == 200
        assert b"<title>" in raw and "控制台".encode() in raw
        assert b":root" in client.call("GET", "/style.css")[1]
        assert b"/api/chat" in client.call("GET", "/app.js")[1]

        # Markdown 渲染器是 ES module：浏览器按 strict MIME 校验，一次都不能退成
        # application/octet-stream（server.py 的 MIME_TYPES 表里没有 .mjs 就是这个下场）
        assert content_type_of(client, "/markdown.mjs") == "text/javascript"
        assert content_type_of(client, "/app.js") == "text/javascript"
        assert b"renderMarkdown" in client.call("GET", "/markdown.mjs")[1]
        assert b"export function" in client.call("GET", "/markdown.mjs")[1]
        assert b'type="module"' in client.call("GET", "/")[1]
        assert b"renderMarkdown" in client.call("GET", "/app.js")[1]
        # 手工验收抓到的漏网：只有历史轮次走渲染器，刚发出的那一轮还是
        # `body.textContent = result.reply`（原始 Markdown），用户先看到一屏
        # 反引号和星号，刷新才正常。历史 + 流式两条路径都得渲染，钉住。
        app_js = client.call("GET", "/app.js")[1]
        assert app_js.count(b"renderMarkdown") >= 3
        assert b"body.textContent = result.reply" not in app_js

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

        # 流式对话：start（带 job_id）→ text_delta…→ result → end
        status, raw = client.call("POST", "/api/chat", body={"text": "帮我安排今天"})
        assert status == 200
        events = parse_sse(raw)
        kinds = [event["kind"] for event in events]
        assert kinds[0] == "start"
        assert kinds[1] == "iteration"
        assert len(events[0]["job_id"]) == 32
        assert kinds[-2:] == ["result", "end"]
        deltas = "".join(e.get("text", "") for e in events if e["kind"] == "text_delta")
        assert deltas == REPLY
        assert events[-2]["reply"] == REPLY

        # 历史面板的四个动作：改名 / 搜索 / 导出 / 删除
        # （放在聊过一轮之后：这三个动作都是"对着已有会话"做的）
        status, raw = client.call(
            "POST",
            "/api/session/rename",
            body={"session_id": "web:default", "title": "今天的事"},
        )
        assert status == 200, raw
        assert json.loads(raw)["sessions"][0]["title"] == "今天的事"

        status, raw = client.call("GET", "/api/sessions/search?q=%E4%BB%8A%E5%A4%A9")
        assert status == 200, raw
        assert [s["session_id"] for s in json.loads(raw)["sessions"]] == ["web:default"]

        # 路由顺序的守门人：/api/session/<id>/export 排在 /api/session/<id> 前缀分支之前，
        # 否则 web:default/export 会被当成一个会话 id
        status, raw = client.call("GET", "/api/session/web:default/export")
        assert status == 200, raw
        assert json.loads(raw)["turns"][0]["user"] == "帮我安排今天"

        status, raw = client.call("GET", "/api/session/web:%E6%B2%A1%E6%9C%89/export")
        assert status == 404 and json.loads(raw)["error"] == "empty_session"

        # 正在用的会话删不掉；不存在的接口如实 404
        status, raw = client.call("DELETE", "/api/session/web:default")
        assert status == 400 and json.loads(raw)["error"] == "session_in_use"
        status, raw = client.call("DELETE", "/api/nope")
        assert status == 404 and json.loads(raw)["error"] == "not_found"

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


def test_upload_accepts_header_name_and_never_overwrites(settings, clock):
    """粘贴 / 拖拽走的是无表单那条通道，而且同一张截图会被传两次。

    浏览器粘贴进来的图片几乎总是叫 ``image.png``——第二张要是覆盖了第一张，用户
    会拿着"刚上传的文件"读到上一张的内容，这种错最难查也最难解释。
    """
    api = make_api(settings, clock)
    server = build_server(settings, port=0, api=api, quiet=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    client = Client(server.url.rstrip("/"))
    try:
        status, raw = client.call(
            "POST",
            "/api/upload",
            body=b"# \xe7\xac\xac\xe4\xb8\x80\xe5\xbc\xa0\n",
            headers={"X-Yixiang-Filename": "image.png"},
        )
        assert status == 200, raw
        first = json.loads(raw)
        assert first["path"].startswith("uploads/")
        assert first["name"].endswith("image.png")
        assert first["hint"] == f'让我读它：read_file(path="{first["path"]}")'
        assert (settings.data_dir / first["path"]).read_bytes() == b"# \xe7\xac\xac\xe4\xb8\x80\xe5\xbc\xa0\n"

        # 第二张同名截图：新文件落新名字，第一张原封不动
        status, raw = client.call(
            "POST",
            "/api/upload",
            body=b"# two\n",
            headers={"X-Yixiang-Filename": "image.png"},
        )
        assert status == 200, raw
        second = json.loads(raw)
        assert second["name"] != first["name"]
        assert "-2" in second["name"]
        assert (settings.data_dir / first["path"]).read_bytes() == b"# \xe7\xac\xac\xe4\xb8\x80\xe5\xbc\xa0\n"

        # 文件名里的路径分隔符在落盘前就被吃掉：uploads/ 外面一个字节都写不出去
        status, raw = client.call(
            "POST",
            "/api/upload",
            body=b"x",
            headers={"X-Yixiang-Filename": "../../evil.md"},
        )
        assert status == 200, raw
        escaped = json.loads(raw)
        assert escaped["path"].startswith("uploads/")
        assert ".." not in escaped["path"]
        assert (settings.data_dir / escaped["path"]).is_file()
    finally:
        server.shutdown()
        server.close_all()
        thread.join(timeout=5)


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


# --------------------------------------------------------------------- 停止生成
class HangingProvider:
    """永远不回的 Provider：给"停止生成"当靶子。

    ``started`` 在**调用真的进来了**之后才 set——用例要等到它，才谈得上"停下来"；
    不用真睡满：能结束这一轮的唯一方式就是 ``task.cancel()``，所以用例一秒都不用等
    （§13.6）。``complete`` 直接断言失败：停止之后不该再发起任何新调用。
    """

    def __init__(self, *, started: threading.Event) -> None:
        self.started = started
        self.calls = 0

    async def complete(self, req: Any) -> Any:  # pragma: no cover - 走到这里就是没停住
        raise AssertionError("停止之后不该再有 complete 调用")

    async def complete_stream(self, req: Any, observer: Any = None) -> Any:
        self.calls += 1
        self.started.set()
        await asyncio.sleep(30)  # 只有取消能让它结束
        raise AssertionError("这一轮本该被取消掉")

    async def aclose(self) -> None:
        return None


def hanging_api(settings, clock, provider) -> ConsoleAPI:
    """与 ``make_api`` 同一套路，只是把 Provider 换成一个**不回**的。"""
    env_file = Path(settings.data_dir).parent / ".env"
    env_file.write_text("YIXIANG_MAIN_MODEL=deepseek-chat\n", encoding="utf-8")
    sink = JsonlUsageSink(settings.usage_path, clock=clock)

    def factory(current):
        return App.from_settings(
            current, provider=provider, usage_sink=sink, clock=clock
        )

    return ConsoleAPI(settings, app_factory=factory, env_file=env_file)


def _open_chat_stream(host: str, port: int, text: str) -> socket.socket:
    """发一条 ``/api/chat`` 并把连接**停在读一半**的状态交回来（SSE 是长连接）。"""
    body = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    sock = socket.create_connection((host, port), timeout=10)
    sock.sendall(
        b"POST /api/chat HTTP/1.1\r\n"
        + f"Host: {host}:{port}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode()
        + b"Connection: close\r\n\r\n"
        + body
    )
    return sock


def _read_until(sock: socket.socket, needle: bytes, *, timeout: float = 10.0) -> bytes:
    """读到出现 ``needle`` 为止（超时或对端关闭就返回已经拿到的部分）。"""
    sock.settimeout(timeout)
    raw = b""
    while needle not in raw:
        chunk = sock.recv(4096)
        if not chunk:
            break
        raw += chunk
    return raw


def _read_all(sock: socket.socket, *, timeout: float = 10.0) -> bytes:
    """读到对端关闭（SSE 收尾时会关连接）。"""
    sock.settimeout(timeout)
    raw = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            break
        if not chunk:
            break
        raw += chunk
    return raw


def _job_id(raw: bytes) -> str:
    match = re.search(rb'"job_id": "([0-9a-f]{32})"', raw)
    assert match, f"SSE 里没有 job_id：{raw!r}"
    return match.group(1).decode()


def test_a_running_turn_can_be_stopped_and_still_leaves_a_trace(settings, clock):
    """「停止生成」= 取消工作线程上那个 task，但这一轮照常落 chat_log 与 trace。

    为什么不能只靠"浏览器断开"：``_stream_chat`` 明确写着断开的连接"这一轮照跑完"，
    也就是钱照花、工具照调。停止必须打到**跑的那一侧**。
    """
    started = threading.Event()
    provider = HangingProvider(started=started)
    api = hanging_api(settings, clock, provider)
    server = build_server(settings, port=0, api=api, quiet=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    client = Client(server.url.rstrip("/"))
    try:
        with _open_chat_stream(host, port, "帮我写一篇长文") as sock:
            head = _read_until(sock, b'"kind": "start"')
            assert b" 200 " in head.split(b"\r\n")[0]  # SSE 已经开流
            job_id = _job_id(head)
            assert started.wait(timeout=10), "这一轮压根没进 Provider"
            assert provider.calls == 1

            status, raw = client.call("POST", f"/api/chat/{job_id}/cancel")
            assert status == 200, raw
            assert json.loads(raw)["cancelled"] is True

            rest = _read_all(sock)

        kinds = [event["kind"] for event in parse_sse(head + rest)]
        assert kinds[0] == "start"
        assert kinds[-3:] == ["result", "cancelled", "end"]
        assert parse_sse(head + rest)[-2]["job_id"] == job_id
        assert parse_sse(head + rest)[-3]["finish_reason"] == "cancelled"

        # 落盘一致性：这一轮在 chat_log 与 trace 里都看得见（换一条连接查，别碰工作线程那条）
        with sqlite3.connect(settings.db_path) as check:
            rows = check.execute(
                "SELECT user_text, reply_text FROM chat_log ORDER BY id"
            ).fetchall()
        assert rows == [("帮我写一篇长文", "（已停止生成）")]
        traces = sorted(settings.traces_dir.glob("*.jsonl"))
        assert traces, "trace 没落盘"
        record = json.loads(traces[-1].read_text(encoding="utf-8").strip().splitlines()[-1])
        assert record["finish_reason"] == "cancelled"
        assert record["user_text"] == "帮我写一篇长文"

        # 取消只打死这一轮，不打死工作线程：串行队列还活着（死了这里会挂到超时）
        status, raw = client.call("GET", "/api/state")
        assert status == 200, raw
    finally:
        server.shutdown()
        server.close_all()
        thread.join(timeout=5)
    assert thread.is_alive() is False


def test_cancelling_a_queued_job_never_reaches_the_provider(settings, clock):
    """排队中的那一轮被停掉：Provider **一次都不该被调用**（钱不能花出去）。"""
    started = threading.Event()
    provider = HangingProvider(started=started)
    api = hanging_api(settings, clock, provider)
    server = build_server(settings, port=0, api=api, quiet=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    client = Client(server.url.rstrip("/"))
    try:
        with _open_chat_stream(host, port, "第一句") as first:
            first_head = _read_until(first, b'"kind": "start"')
            assert started.wait(timeout=10)
            with _open_chat_stream(host, port, "第二句") as second:
                second_head = _read_until(second, b'"kind": "start"')
                second_id = _job_id(second_head)
                assert provider.calls == 1, "第二条不该已经进 Provider"

                status, raw = client.call("POST", f"/api/chat/{second_id}/cancel")
                assert status == 200, raw
                assert json.loads(raw)["cancelled"] is True

                second_rest = _read_all(second)
                assert provider.calls == 1, "被停掉的排队轮次不该碰 Provider"
                second_kinds = [e["kind"] for e in parse_sse(second_head + second_rest)]
                assert second_kinds == ["start", "cancelled", "end"]

            # 收尾：第一条也停掉，别让它拖着（30 秒的 sleep 会在取消时立刻结束）
            status, raw = client.call("POST", f"/api/chat/{_job_id(first_head)}/cancel")
            assert status == 200, raw
            _read_all(first)
    finally:
        server.shutdown()
        server.close_all()
        thread.join(timeout=5)
    assert provider.calls == 1
    assert thread.is_alive() is False


def test_cancelling_an_unknown_job_is_a_404(settings, clock):
    """停止一个不存在 / 已经结束的 job：如实回 404，不能静默回一个"停好了"。"""
    api = make_api(
        settings, clock, text_reply(REPLY), env_text="YIXIANG_MAIN_MODEL=deepseek-chat\n"
    )
    server = build_server(settings, port=0, api=api, quiet=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    client = Client(server.url.rstrip("/"))
    try:
        status, raw = client.call("POST", "/api/chat/0123456789abcdef0123456789abcdef/cancel")
        assert status == 404
        assert json.loads(raw)["error"] == "unknown_job"
        # 不是这条路由的其它 /api/chat/ 路径仍然是 404（别把路由写成前缀通吃）
        status, raw = client.call("POST", "/api/chat/nope")
        assert status == 404 and json.loads(raw)["error"] == "not_found"
    finally:
        server.shutdown()
        server.close_all()
        thread.join(timeout=5)


def test_cancelling_twice_is_404_the_second_time(settings, clock):
    """同一个 job 停两次：第二次是 404 —— 说明"在册"这件事真的被回收了。"""
    started = threading.Event()
    provider = HangingProvider(started=started)
    api = hanging_api(settings, clock, provider)
    server = build_server(settings, port=0, api=api, quiet=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    client = Client(server.url.rstrip("/"))
    try:
        with _open_chat_stream(host, port, "帮我写一篇长文") as sock:
            head = _read_until(sock, b'"kind": "start"')
            job_id = _job_id(head)
            assert started.wait(timeout=10)
            assert client.call("POST", f"/api/chat/{job_id}/cancel")[0] == 200
            _read_all(sock)
        status, raw = client.call("POST", f"/api/chat/{job_id}/cancel")
        assert status == 404 and json.loads(raw)["error"] == "unknown_job"
    finally:
        server.shutdown()
        server.close_all()
        thread.join(timeout=5)


# --------------------------------------------------------------------- 链路
def test_trace_list_and_detail_round_trip(settings, clock):
    # 剧本里带上模型与 token：列表要显示的"迭代 / tokens / 成本"才有真数字可循
    api = make_api(
        settings,
        clock,
        text_reply(REPLY, model="deepseek-chat", usage=usage(120, 40, cached=64)),
    )
    result = api.chat("今天读什么")

    listing = api.traces()
    assert listing["count"] == 1
    row = listing["traces"][0]
    assert row["turn_id"] == result["turn_id"]
    assert row["session"] == "web:default"
    assert row["source"] == "web"
    assert row["finish_reason"] == "stop"
    assert row["tools"] == []
    assert row["tokens"]["out"] > 0
    assert row["cost_cny"] >= 0.0
    assert row["error"] in (None, "")

    # 详情比列表多：用户原话、回复预览、模型、工作记忆分段
    detail = api.trace(result["turn_id"])
    assert detail["turn_id"] == result["turn_id"]
    assert detail["user_text"] == "今天读什么"
    assert detail["reply_preview"] == REPLY
    assert detail["model"] == "deepseek-chat"
    assert "s1" in detail["working_memory"]

    with pytest.raises(ConsoleError) as exc:
        api.trace("  ")
    assert (exc.value.status, exc.value.code) == (400, "bad_turn")

    with pytest.raises(ConsoleError) as exc:
        api.trace("t_20260101_000000_dead")
    assert (exc.value.status, exc.value.code) == (404, "no_trace")
    assert "找不到" in exc.value.message


def test_http_layer_exposes_traces_and_404s_unknown_turn(settings, clock):
    # 先起服务再聊：``sqlite3`` 的连接归跑它的线程所有，用例线程只当客户端
    # （在主线程上先 ``api.chat`` 再 ``close_all``，会在 runner 线程上撞 cross-thread）
    api = make_api(settings, clock, text_reply(REPLY))
    server = build_server(settings, port=0, api=api, quiet=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    client = Client(server.url.rstrip("/"))
    try:
        status, raw = client.call("POST", "/api/chat", body={"text": "今天读什么"})
        assert status == 200
        result = [e for e in parse_sse(raw) if e["kind"] == "result"][-1]
        turn_id = result["turn_id"]

        status, raw = client.call("GET", "/api/traces?limit=5")
        assert status == 200
        listing = json.loads(raw)
        assert listing["count"] == 1
        assert listing["traces"][0]["turn_id"] == turn_id

        status, raw = client.call("GET", f"/api/trace/{turn_id}")
        assert status == 200
        assert json.loads(raw)["session"] == "web:default"

        # 未知 turn_id：404 是"查无此物"，不是"服务器炸了"
        status, raw = client.call("GET", "/api/trace/t_20260101_000000_dead")
        assert status == 404
        assert json.loads(raw)["error"] == "no_trace"
    finally:
        server.shutdown()
        server.close_all()
        thread.join(timeout=5)


def test_http_layer_lists_deletes_and_serves_uploaded_images(settings, clock):
    """上传件在 HTTP 层上的四个新动作：列（刷新后还在）、缩略图、删一个、清空。

    顺带把"只带图不带字"的一轮打穿：前端 ``sendMessage`` 发的就是 ``{text, images}``
    这个体，它必须在**开流之前**被接住——否则图片轮次会先拿到 200，再被当成空消息。
    """
    api = make_api(settings, clock, text_reply("看到图了"))
    server = build_server(settings, port=0, api=api, quiet=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    client = Client(server.url.rstrip("/"))
    try:
        body, content_type = multipart_body("shot.png", PNG_1X1)
        status, raw = client.call(
            "POST", "/api/upload", body=body, headers={"Content-Type": content_type}
        )
        assert status == 200, raw
        png = json.loads(raw)
        assert png["kind"] == "image"
        assert png["url"] == f"/api/uploads/{png['name']}/raw"

        body, content_type = multipart_body("notes.md", "# 笔记".encode())
        status, raw = client.call(
            "POST", "/api/upload", body=body, headers={"Content-Type": content_type}
        )
        assert status == 200, raw
        note = json.loads(raw)

        # 列表来自磁盘：两个都在，按文件名倒序（同一天里 shot > notes）
        status, raw = client.call("GET", "/api/uploads")
        assert status == 200, raw
        listing = json.loads(raw)
        assert [item["path"] for item in listing["items"]] == [png["path"], note["path"]]
        assert listing["limit"] == MAX_UPLOAD_BYTES

        # 缩略图：真字节 + 真类型（前端 <img src=item.url> 拿的就是它）
        status, raw = client.call("GET", png["url"])
        assert status == 200 and raw == PNG_1X1
        assert content_type_of(client, png["url"]) == "image/png"

        # 路由顺序的守门人：/api/uploads/<name>/raw 不能被当成一个文件名去查
        status, raw = client.call("GET", "/api/uploads/nope.png/raw")
        assert status == 404 and json.loads(raw)["error"] == "upload_not_found"

        # 只带图不带字：合法一轮——"看看这张"这句话本身可以省掉
        status, raw = client.call(
            "POST", "/api/chat", body={"text": "", "images": [png["path"]]}
        )
        assert status == 200, raw
        assert [event["kind"] for event in parse_sse(raw)][-2:] == ["result", "end"]
        assert api.app.provider.requests[-1].messages[-1].images == [png["path"]]

        # 一个字都没有、也没附图，才是空消息
        status, raw = client.call("POST", "/api/chat", body={"text": "   "})
        assert status == 400 and json.loads(raw)["error"] == "empty_message"

        # 工作区里没有的图：这一轮报错并说清是哪一张（静默丢掉会让"看图"的结论
        # 无从核对）。它和 no_api_key 一样是"开流之后才发现的错"，走 SSE 的
        # error 事件；前端 app.js 的 case "error" 正是接它。
        status, raw = client.call(
            "POST", "/api/chat", body={"text": "看图", "images": ["uploads/nope.png"]}
        )
        assert status == 200
        failure = [event for event in parse_sse(raw) if event["kind"] == "error"][-1]
        assert failure["code"] == "bad_image"
        assert "nope.png" in failure["message"]

        # 删一个：剩下的还在；不存在的名字如实 404
        status, raw = client.call("DELETE", f"/api/uploads/{png['name']}")
        assert status == 200, raw
        assert json.loads(raw)["deleted"] == png["name"]
        assert [item["path"] for item in json.loads(raw)["items"]] == [note["path"]]

        status, raw = client.call("DELETE", "/api/uploads/nope.md")
        assert status == 404 and json.loads(raw)["error"] == "upload_not_found"

        # 清空：/api/uploads/clear 是"清空"，不是"删掉一个叫 clear 的文件"
        status, raw = client.call("POST", "/api/uploads/clear")
        assert status == 200, raw
        assert json.loads(raw)["deleted"] == 1
        assert json.loads(client.call("GET", "/api/uploads")[1])["items"] == []
        assert json.loads(client.call("POST", "/api/uploads/clear")[1])["deleted"] == 0
    finally:
        server.shutdown()
        server.close_all()
        thread.join(timeout=5)
