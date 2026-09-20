"""不可信外部内容的统一包裹（TECH §14.3-2、T-2/T-3）。

原则一句话：**模型可以建议，但只有代码做决定**。影视简介、检索片段、将来的网页
正文都属于"模型读得到但不可信"的输入面，它们进 prompt 前必须被标签包住，并在
``soul.md`` 的守则里被声明为"数据不是指令"。

包裹标签只有一处定义（这里），出口各自声明 ``source``：语料是 ``media_db``、
长期记忆检索是 ``memory``。散落在各处的字面量迟早会漂移，漂移就等于防线失效。
"""

from __future__ import annotations

CLOSE_TAG = "</external_content>"


def open_tag(source: str) -> str:
    return f'<external_content source="{source}">'


def wrap_external(text: str, *, source: str = "external") -> str:
    """把一段不可信文本包成外部内容（进 prompt 的唯一出口）。"""
    return f"{open_tag(source)}\n{text}\n{CLOSE_TAG}"


def is_wrapped(text: str) -> bool:
    """判断一段文本是否已经被包裹（避免重复嵌套）。"""
    body = (text or "").strip()
    return body.startswith("<external_content ") and body.endswith(CLOSE_TAG)


__all__ = ["CLOSE_TAG", "is_wrapped", "open_tag", "wrap_external"]
