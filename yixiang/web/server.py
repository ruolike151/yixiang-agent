"""HTTP 层：路由、JSON、SSE 与静态文件（TECH §10，ADR-3）。

这一层只做协议：路径 → ``ConsoleAPI`` 的某个方法 → 响应。**没有业务判断**——
"这个字段能不能改""超没超上限"全在 ``console.py``，所以"网页上做不了的事"和
"命令行做不了的事"是同一套规则。

两条实现上的取舍写在明处：

  * **只用标准库**：``ThreadingHTTPServer`` 足够跑一个本机测试台，少一个依赖就
    少一处"装不上 / 版本冲突"。默认只绑 ``127.0.0.1``——这是本机工具，不是服务端。
  * **业务调用串行化**：``sqlite3`` 的连接不能跨线程用（``db.py`` 的纪律），所以
    HTTP 可以多线程接连接，真正碰 ``App`` 的调用统一排进**一条工作线程**。流式
    对话的事件由这条线程产出、由请求线程写回浏览器，两边各归各的线程。
"""

from __future__ import annotations

import json
import queue
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from yixiang.config import Settings
from yixiang.runtime import eventloop
from yixiang.web.console import MAX_UPLOAD_BYTES, ConsoleAPI, ConsoleError

# 前端文件（手写、无构建）：server.py 只负责按路径读出来
STATIC_DIR = Path(__file__).resolve().parent / "static"
JSON_TYPE = "application/json; charset=utf-8"
SSE_TYPE = "text/event-stream; charset=utf-8"
# JSON 请求体的上限（人设 / 记忆全文也在这个量级，够了）
JSON_BODY_LIMIT = 400_000
# multipart 的分隔行 / 头部开销：真正的上限在 console.upload 里按文件字节数把
MULTIPART_SLACK = 64 * 1024
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}


# --------------------------------------------------------------------- 工作线程
@dataclass
class _Job:
    """一次业务调用：排队进去，结果 / 异常带回来（像 Future，但不引 concurrent）。"""

    fn: Any
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    done: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: BaseException | None = None

    def wait(self, timeout: float | None = None) -> _Job:
        self.done.wait(timeout)
        return self


class _SerialRunner:
    """唯一碰 ``App`` 的那条线程（``sqlite3`` 连接归它独有）。

    HTTP 侧可以是多线程的——多个连接同时进来只会让调用在这里**排队**，
    而不会出现"连接 A 建的 sqlite 连接被线程 B 拿去用"。
    """

    def __init__(self, api: ConsoleAPI) -> None:
        self.api = api
        self._queue: queue.Queue[_Job | None] = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop, name="yixiang-web-worker", daemon=True
        )
        self._thread.start()

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> _Job:
        job = _Job(fn=fn, args=args, kwargs=kwargs)
        self._queue.put(job)
        return job

    def call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        job = self.submit(fn, *args, **kwargs)
        job.wait()
        if job.error is not None:
            raise job.error
        return job.value

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        try:
            while True:
                job = self._queue.get()
                if job is None:
                    return
                try:
                    job.value = job.fn(*job.args, **job.kwargs)
                except Exception as exc:  # noqa: BLE001 - 异常要带回请求线程，不能吞
                    job.error = exc
                finally:
                    job.done.set()
        finally:
            # 这条线程走了，它那条常驻事件循环也归它关（runtime/eventloop.py）
            eventloop.shutdown()


# --------------------------------------------------------------------- 请求处理
class ConsoleHandler(BaseHTTPRequestHandler):
    """把 ``ConsoleAPI`` 摊成 REST：读是 GET、写是 PUT/POST、对话是 SSE。"""

    server_version = "YixiangConsole/0.1"
    protocol_version = "HTTP/1.1"
    server: ConsoleServer  # type: ignore[assignment] - 由 ConsoleServer 注入口

    # ------------------------------------------------------------- 动词入口
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定名
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 父类签名
        if self.server.quiet:
            return
        super().log_message(format, *args)

    # ------------------------------------------------------------- 分发
    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/") or "/"
        self._responded = False
        try:
            if path == "/api" or path.startswith("/api/"):
                self._route_api(method, path, parse_qs(parsed.query))
            elif method in {"GET", "HEAD"}:
                self._send_static(path, head=method == "HEAD")
            else:
                raise ConsoleError(
                    f"{method} {path} 不支持（静态文件只读，接口在 /api 下）",
                    status=405,
                    code="method_not_allowed",
                )
        except ConsoleError as exc:
            self._send_error(exc)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - 兜底：如实回错，别留一个挂住的连接
            self._send_error(
                ConsoleError(f"服务内部错误：{exc}", status=500, code="internal")
            )

    def _route_api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        api = self.server.api
        runner = self.server.runner

        if method in {"GET", "HEAD"}:
            if path == "/api/state":
                return self._send_json(runner.call(api.state))
            if path == "/api/sessions":
                return self._send_json(runner.call(api.sessions, _int(query, "limit", 30)))
            if path == "/api/session":
                return self._send_json(
                    runner.call(
                        api.transcript, _one(query, "id"), _int(query, "limit", 200)
                    )
                )
            if path.startswith("/api/session/"):
                session_id = path[len("/api/session/") :]
                return self._send_json(
                    runner.call(api.transcript, session_id, _int(query, "limit", 200))
                )
            if path == "/api/persona":
                return self._send_json(runner.call(api.persona))
            if path == "/api/memory":
                return self._send_json(runner.call(api.memory))
            if path == "/api/config":
                return self._send_json(runner.call(api.config))
            if path == "/api/qq":
                return self._send_json(runner.call(api.qq))
            if path == "/api/prompt":
                return self._send_json(runner.call(api.prompt))
            if path == "/api/tools":
                return self._send_json(runner.call(api.tools))
            if path == "/api/skills":
                return self._send_json(runner.call(api.skills))

        if method == "POST":
            if path == "/api/session/switch":
                body = self._json_body()
                session_id = str(body.get("session_id") or body.get("id") or "")
                return self._send_json(runner.call(api.switch, session_id))
            if path == "/api/session/new":
                body = self._json_body()
                name = str(body.get("name") or "").strip()
                return self._send_json(runner.call(api.new_session, name or None))
            if path == "/api/memory/sync":
                return self._send_json(runner.call(api.sync_memory))
            if path == "/api/chat":
                return self._stream_chat()
            if path == "/api/upload":
                return self._upload()

        if method == "PUT":
            body = self._json_body()
            if path == "/api/persona":
                return self._send_json(
                    runner.call(
                        api.save_persona,
                        str(body.get("name") or ""),
                        str(body.get("text") or ""),
                    )
                )
            if path == "/api/memory":
                return self._send_json(
                    runner.call(api.save_memory, str(body.get("text") or ""))
                )
            if path == "/api/config":
                return self._send_json(runner.call(api.save_config, body))
            if path == "/api/qq":
                return self._send_json(runner.call(api.save_qq, body))

        raise ConsoleError(f"没有这个接口：{method} {path}", status=404, code="not_found")

    # ------------------------------------------------------------- 对话（SSE）
    def _stream_chat(self) -> None:
        body = self._json_body()
        # 空消息在开流之前就挡掉：这样前端拿到的是一条干净的 400 JSON，而不是
        # "已经 200 了才告诉我没内容"。
        text = str(body.get("text") or "")
        if not text.strip():
            raise ConsoleError("消息是空的：写一句再发送。", code="empty_message")

        events: queue.Queue[dict[str, Any]] = queue.Queue()
        job = self.server.runner.submit(self.server.api.chat, text, events.put)
        self._begin_sse()
        try:
            while True:
                try:
                    event = events.get(timeout=0.05)
                except queue.Empty:
                    if job.done.is_set() and events.empty():
                        break
                    continue
                self._sse_send(event)
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器关了页面：这一轮照跑完（trace 与 chat_log 要落盘），只是没人看
            self.close_connection = True
        finally:
            job.wait()
        if job.error is not None:
            self._sse_send(_error_event(job.error))
        self._sse_send({"kind": "end"})

    def _begin_sse(self) -> None:
        self._responded = True
        self.send_response(200)
        self.send_header("Content-Type", SSE_TYPE)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b": yixiang web console\n\n")
        self.wfile.flush()

    def _sse_send(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False, default=str)
        self.wfile.write(f"data: {payload}\n\n".encode())
        self.wfile.flush()

    # ------------------------------------------------------------- 上传
    def _upload(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        raw = self._body_bytes(limit=MAX_UPLOAD_BYTES + MULTIPART_SLACK)
        if "multipart/form-data" in content_type.lower():
            parts = parse_multipart(content_type, raw)
            picked = next((part for part in parts if part["filename"]), None)
            if picked is None:
                raise ConsoleError(
                    "上传的表单里没有带文件名的字段：选一个文件再传。",
                    code="bad_multipart",
                    payload={"fields": [part["name"] for part in parts]},
                )
            filename, data = str(picked["filename"]), picked["data"]
        else:
            # 无浏览器的场合（curl / 脚本）：原样传字节，文件名叫走请求头
            filename = self.headers.get("X-Yixiang-Filename", "")
            if not filename:
                raise ConsoleError(
                    "上传需要一个文件名：用 multipart 表单，或带上 X-Yixiang-Filename 头。",
                    code="bad_multipart",
                )
            data = raw
        self._send_json(self.server.runner.call(self.server.api.upload, filename, data))

    # ------------------------------------------------------------- 响应
    def _send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(data, content_type=JSON_TYPE, status=status)

    def _send_bytes(
        self,
        data: bytes,
        *,
        content_type: str,
        status: int = 200,
        head: bool = False,
    ) -> None:
        if self._responded:
            return
        self._responded = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _send_error(self, exc: ConsoleError) -> None:
        if self._responded:  # SSE 已经开流：错误走事件，不再发 HTTP 状态码
            return
        payload = {"error": exc.code, "message": exc.message, **exc.payload}
        self._send_json(payload, status=exc.status)

    def _send_static(self, path: str, *, head: bool = False) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = _resolve_static(relative)
        if target is None:
            raise ConsoleError(
                f"没有这个页面：{path}（前端入口是 /）", status=404, code="not_found"
            )
        content_type = MIME_TYPES.get(
            target.suffix.lower(), "application/octet-stream"
        )
        self._send_bytes(target.read_bytes(), content_type=content_type, head=head)

    # ------------------------------------------------------------- 请求体
    def _body_bytes(self, *, limit: int | None = None) -> bytes:
        # chunked 请求没有 Content-Length，而本机控制台只按 Content-Length 读体。
        # 与其让 multipart 解析器报"没有带文件名的字段"（把调用方引向错误的方向），
        # 不如在这里就说清楚：换浏览器或 curl -F 再传。
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            raise ConsoleError(
                "这个请求用的是 chunked 传输（没有 Content-Length）：本机控制台只接受带 "
                "Content-Length 的请求，换浏览器或 curl -F 再传。",
                status=411,
                code="chunked_not_supported",
            )
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return b""
        try:
            size = int(raw_length)
        except ValueError as exc:
            raise ConsoleError("Content-Length 不是数字。", code="bad_length") from exc
        if limit is not None and size > limit:
            self.close_connection = True  # 剩下的字节不读了：这条连接不能复用
            raise ConsoleError(
                f"请求体 {size} 字节，超过上限 {limit} 字节。",
                status=413,
                code="too_large",
                payload={"limit": limit, "actual": size},
            )
        return self.rfile.read(size) if size > 0 else b""

    def _json_body(self) -> dict[str, Any]:
        raw = self._body_bytes(limit=JSON_BODY_LIMIT)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConsoleError(
                f"请求体不是合法 JSON：{exc}", code="bad_json"
            ) from exc
        if not isinstance(data, dict):
            raise ConsoleError("请求体应该是一个 JSON 对象。", code="bad_json")
        return data


class ConsoleServer(ThreadingHTTPServer):
    """一个进程一个实例：持有 ``ConsoleAPI`` 与它唯一的工作线程。"""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], api: ConsoleAPI, *, quiet: bool = False
    ) -> None:
        super().__init__(address, ConsoleHandler)
        self.api = api
        self.quiet = quiet
        self.runner = _SerialRunner(api)

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}/"

    def warmup(self) -> None:
        """在工作线程里先装配一次 ``App``：坏 ``.env`` / 缺依赖在启动时就报出来。"""
        self.runner.call(lambda: self.api.app)

    def close_all(self) -> None:
        """关服务：先让工作线程把 ``App`` 关掉（连接归它所有），再收线程。"""
        try:
            self.runner.call(self.api.close)
        finally:
            self.runner.stop()
            self.server_close()


# --------------------------------------------------------------------- 对外入口
def build_server(
    settings: Settings,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    api: ConsoleAPI | None = None,
    quiet: bool = False,
) -> ConsoleServer:
    """建一个可 ``serve_forever()`` 的服务器（``port=0`` 时由系统挑端口，供用例用）。"""
    server = ConsoleServer((host, port), api or ConsoleAPI(settings), quiet=quiet)
    server.warmup()
    return server


def serve(
    settings: Settings,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    out: Any = print,
) -> int:
    """``yixiang web`` 的实现：前台跑，Ctrl+C 退出。"""
    server = build_server(settings, host=host, port=port)
    try:
        out(f"yixiang web 控制台：{server.url}")
        out(f"  {settings.describe()}")
        out("  只绑本机回环地址；测试用前端共「对话 / 历史 / 人设与记忆 / 模型配置 / 提示词 / QQ 设置」六栏")
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        out("\n收到 Ctrl+C，正在关闭…")
    finally:
        server.close_all()
    return 0


# --------------------------------------------------------------------- 辅助
def parse_multipart(content_type: str, body: bytes) -> list[dict[str, Any]]:
    """够用的 multipart/form-data 解析（浏览器 ``FormData`` 那一种）。

    只做一件事：按 boundary 切段，从每段的 ``Content-Disposition`` 里取
    ``name`` / ``filename``。不引第三方库，也不处理嵌套 multipart——
    测试台上传一个文件用不上那些。
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if match is None:
        raise ConsoleError(
            "multipart 请求缺少 boundary，换个浏览器或直接 curl 再试。",
            code="bad_multipart",
        )
    delimiter = b"--" + match.group(1).strip().encode("utf-8", "replace")
    parts: list[dict[str, Any]] = []
    for chunk in body.split(delimiter):
        if not chunk.strip() or chunk.strip() == b"--":
            continue
        chunk = chunk.strip(b"\r\n")
        head, separator, data = chunk.partition(b"\r\n\r\n")
        if not separator:
            continue
        headers = head.decode("utf-8", "replace")
        name = re.search(r'name="([^"]*)"', headers)
        if name is None:
            continue
        filename = re.search(r'filename="([^"]*)"', headers)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        parts.append(
            {
                "name": name.group(1),
                "filename": filename.group(1) if filename else None,
                "content_type": _header_value(headers, "content-type"),
                "data": data,
            }
        )
    return parts


def _header_value(headers: str, key: str) -> str:
    for line in headers.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip().lower() == key:
            return value.strip()
    return ""


def _resolve_static(relative: str) -> Path | None:
    """把相对路径收敛在 ``static/`` 之内（``../`` 与软链接都逃不出去）。"""
    root = STATIC_DIR.resolve()
    candidate = (root / relative).resolve()
    if root not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def _error_event(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ConsoleError):
        return {"kind": "error", "code": exc.code, "message": exc.message, **exc.payload}
    return {"kind": "error", "code": "internal", "message": f"这一轮出错：{exc}"}


def _one(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int(_one(query, key) or default)
    except ValueError:
        return default


__all__ = ["ConsoleServer", "build_server", "parse_multipart", "serve"]
