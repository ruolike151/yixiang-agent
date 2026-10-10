"""yixiang（以湘）——本地优先的个人 Agent。

包内模块划分见 docs/TECH-DESIGN.md §1.4：
    config        配置（Settings，字段名与 YIXIANG_* env 一一对应）
    providers     ChatModel 协议 + OpenAI-compatible 实现（角色路由 / 重试 / usage 记账）
    runtime/      会话与内部数据结构（Message / ModelReply / TurnResult / SessionManager）
    loop/         Agent Loop 与 guard
    tools/        工具注册表与具体工具
    gateway/      入口（CLI；QQ 为 P2）
    ops/          trace / usage / 渲染

还有一件必须在**这个包的 import 期**就做完的事：把进程级的那份 MSVCP140 钉在系统
版本上（见 ``runtime/msvc_runtime.py`` 的模块说明）。它得赶在任何 ``import winrt``
之前发生——winrt 自带一份 14.29 的旧运行库，被它先抢到进程里的话，后加载的
onnxruntime（fastembed）会当场把整个进程带走。
"""

from yixiang.runtime.msvc_runtime import unify_msvc_runtime

__version__ = "0.1.0"

# 结果留个名字：`yixiang doctor` / 用例想看一眼时不用再自己跑一遍（幂等，不会再加载）
MSVC_RUNTIME = unify_msvc_runtime()

__all__ = ["__version__", "MSVC_RUNTIME"]
