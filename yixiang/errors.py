"""统一错误码（TECH-DESIGN §11.3）。

约定：
  * 错误码以 ``E_`` 前缀开头，进 trace 的 ``error`` 字段；
  * 每个错误码都配一条"用户可见"的文案——loop / gateway 只能回这句话，
    不允许把异常栈或原始 HTTP 响应体直接给用户（§14.3 第 5 层：输出管控）。
"""

from __future__ import annotations

E_LLM_TIMEOUT = "E_LLM_TIMEOUT"
E_LLM_AUTH = "E_LLM_AUTH"
E_LLM_TRUNCATED = "E_LLM_TRUNCATED"
E_LLM_BAD_REQUEST = "E_LLM_BAD_REQUEST"
E_TOOL_FAILED = "E_TOOL_FAILED"
E_MEMORY_WRITE = "E_MEMORY_WRITE"
E_GATE_FAIL_OPEN = "E_GATE_FAIL_OPEN"
E_QQ_DISCONNECTED = "E_QQ_DISCONNECTED"
E_DB_LOCKED = "E_DB_LOCKED"
E_EMBED_UNAVAILABLE = "E_EMBED_UNAVAILABLE"

USER_MESSAGES: dict[str, str] = {
    E_LLM_TIMEOUT: "我这边超时了，请再说一次",
    E_LLM_AUTH: "我的模型配置有问题，需要你检查 .env",
    E_LLM_TRUNCATED: "（回答比较长，我分两段说；回一句「继续」我接着说下面的）",
    E_LLM_BAD_REQUEST: "我这边调用模型出错了（参数问题），请再试一次",
    E_TOOL_FAILED: "这个操作失败了：{detail}",
    E_MEMORY_WRITE: "抱歉，这条我没记住（{detail}）",
    E_GATE_FAIL_OPEN: "",  # 用户无感，仅 trace
    E_QQ_DISCONNECTED: "",  # 用户无感，自动重连
    E_DB_LOCKED: "我这边忙不过来了，请再试一次",
    E_EMBED_UNAVAILABLE: "",  # 用户无感，降级为纯关键词检索
}


def user_message(code: str, **kwargs: object) -> str:
    """把错误码翻译成给用户看的一句话（未知错误码退化为通用文案）。"""
    template = USER_MESSAGES.get(code)
    if template is None:
        return "我这边出错了，请再试一次"
    try:
        return template.format(**kwargs)
    except KeyError:  # pragma: no cover - 防御性分支
        return template


class YixiangError(Exception):
    """带错误码的基类异常。"""

    code = "E_UNKNOWN"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code

    @property
    def user_message(self) -> str:
        return user_message(self.code, detail=str(self))


class ProviderError(YixiangError):
    """LLM 调用失败。``retryable`` 决定是否进入重试矩阵（§4.3）。"""

    code = "E_LLM_BAD_REQUEST"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None,
                 retryable: bool = False) -> None:
        super().__init__(message, code=code)
        self.status = status
        self.retryable = retryable


class ToolError(YixiangError):
    """工具层错误。注意：工具**不允许**把异常抛给 loop（§9.1），
    这个异常只在注册表内部与测试里使用。"""

    code = "E_TOOL_FAILED"


class SecurityError(YixiangError):
    """路径逃逸 / 越权工具调用等安全拒绝（§9.4）。"""

    code = "E_TOOL_FAILED"
