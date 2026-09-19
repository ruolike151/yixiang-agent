"""yixiang（以湘）——本地优先的个人 Agent。

包内模块划分见 docs/TECH-DESIGN.md §1.4：
    config        配置（Settings，字段名与 YIXIANG_* env 一一对应）
    providers     ChatModel 协议 + OpenAI-compatible 实现（角色路由 / 重试 / usage 记账）
    runtime/      会话与内部数据结构（Message / ModelReply / TurnResult / SessionManager）
    loop/         Agent Loop 与 guard
    tools/        工具注册表与具体工具
    gateway/      入口（CLI；QQ 为 P2）
    ops/          trace / usage / 渲染
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
