"""记忆管理工具（TECH §9.2）：save_memory / manage_memory / update_soul /
update_user / create_skill。

这一层只做三件事：**描述写清什么时候不用**（模型唯一的决策依据）、
**参数先挡一遍**（缺 id / 缺 content 的错误要能照着改）、
**记忆未装配时返回 Error 而不是抛异常**（工具异常不炸 loop 是底线，
但"为什么不可用"要写在给模型看的文本里）。

真正的实现都在 ``yixiang.memory.memory_admin``；这里不碰 SQL。
"""

from __future__ import annotations

from yixiang import memory
from yixiang.memory import memory_admin

_NOT_CONFIGURED = "Error: 记忆子系统未装配（App 未初始化），本工具当前不可用"


def save_memory(subject: str, content: str) -> str:
    """把一条持久事实写进长期记忆（自动去重 + 双写 memory.md）。"""
    if not memory.is_configured():
        return _NOT_CONFIGURED
    return memory_admin.save_memory(subject, content)


def manage_memory(
    action: str = "",
    *,
    id: int | None = None,
    query: str = "",
    content: str = "",
    subject: str = "",
) -> str:
    """搜索 / 更新 / 删除 / 恢复记忆条目。"""
    if not memory.is_configured():
        return _NOT_CONFIGURED
    return memory_admin.manage_memory(
        action, id=id, query=query, content=content, subject=subject
    )


def update_soul(rule: str) -> str:
    """往 soul.md 的 ``## Learned rules`` 追加一条规则（只追加，不删改）。"""
    if not memory.is_configured():
        return _NOT_CONFIGURED
    return memory_admin.update_soul(rule)


def update_user(section: str, content: str) -> str:
    """往 user.md 的某个分区追加一行（超 4000 字符整份拒绝）。"""
    if not memory.is_configured():
        return _NOT_CONFIGURED
    return memory_admin.update_user(section, content)


def create_skill(
    slug: str,
    name: str,
    description: str,
    triggers: list[str] | str,
    body: str,
    confirm: bool = False,
) -> str:
    """写 ``data/skills/<slug>/SKILL.md``（不覆盖已有；必须 confirm=true）。"""
    if not memory.is_configured():
        return _NOT_CONFIGURED
    return memory_admin.create_skill(
        slug, name, description, triggers, body, confirm=confirm
    )


__all__ = [
    "create_skill",
    "manage_memory",
    "save_memory",
    "update_soul",
    "update_user",
]
