"""Web 控制台（本地测试前端，TECH §10 的第三个入口）。

分工只有两句话：

  * ``console.py``——业务适配层：把"看人设 / 改提示词 / 换模型 / 传文件 / 流式对话"
    翻译成对 ``App`` 与 ``data/`` 的调用，本身不做 HTTP；
  * ``server.py``——HTTP 层：路由、JSON、SSE、静态文件，不碰业务判断（ADR-3）。

入口只搬文本这条纪律在这里同样成立：对话链路只有 ``App.handle_message()`` 一条，
Web 控制台不会长出自己的第二份 loop。
"""

from __future__ import annotations

from yixiang.web.console import ConsoleAPI, ConsoleError
from yixiang.web.server import build_server, serve

__all__ = ["ConsoleAPI", "ConsoleError", "build_server", "serve"]
