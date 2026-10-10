"""进程内的 MSVC 运行库对齐：一个进程只能有一份 ``MSVCP140.dll``。

**为什么需要这一层**（2026-10-10 的真事，不是预防性抽象）：

``winrt``（SMTC「正在播放」用它）在包目录里自带一份 **VS2019 时代**的
``MSVCP140.dll``（本机实测 14.29.30157.0），而 CPython 加载扩展模块走的是
``LOAD_WITH_ALTERED_SEARCH_PATH``——**先看 .pyd 自己所在的目录**。于是只要
``import winrt`` 发生过，进程里那份 ``MSVCP140`` 就被钉死在 14.29，
连后加载的 onnxruntime 也照样拿它。

而 onnxruntime（fastembed 的后端）是 VS2022 编的，要 14.4x 的运行库。旧运行库喂给
新二进制 = 访问冲突（``0xC0000005``）——**原生崩溃，Python 拦不住**：
``FastEmbedEmbedder._load()`` 外面那圈 ``except Exception`` 一行都来不及跑。
现场表现就是「聊着聊着整个进程没了」：那一轮 trace 一个字都没有，日志一片干净，
只在 Windows 事件日志里留一条 WER 的 ``AppCrash``（故障模块正是 winrt 目录下那份
``MSVCP140.dll``，异常偏移 ``0x13080``）。

时间线对得上：onnxruntime 09-19 装好、一直没事；winrt 10-06 随「正在播放」进来；
10-10 晚上用户一句要走向量检索的话（「搜索番剧」）把 fastembed 叫醒，进程当场死。

**为什么是「先加载系统那份」**：加载器认的是**模块名**，同名已加载就不会再去
winrt 目录里翻。系统那份（``System32``，本机 14.50）比自带的新，而 MSVC 运行库
向后兼容——新运行库能跑旧二进制。于是 winrt 与 onnxruntime 拿到同一份够新的运行库，
import 的先后顺序从此不再要紧。

**降级口径**与 music / memory / rag 一致：不是 Windows、找不到系统运行库、加载失败，
都只是「不做这件事」并返回一个状态字符串，绝不抛。但那样等于把崩溃留在原地，
所以真有风险时往 stderr 留一行——别再让它变成一条没有日志的悬案。
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
from pathlib import Path

# 幂等：一个进程只做一次；结果同时缓存下来，用例可以直接断言
_DONE: str | None = None

# 钉住那份运行库的句柄：函数返回后没人引用它，留个名字更稳（也顺手说明「这是故意的」）。
# 标注成 object 是为了让这个模块在非 Windows 上也能 import —— 那边没有 WinDLL 这个名字。
_PINNED: list[object] = []


def _candidates() -> list[Path]:
    """系统运行库的搜索顺序。

    ``System32`` 优先（32 位进程由 WOW64 重定向到 ``SysWOW64``，所以不用自己判断位数）；
    后面两条是「装了 Python 但没装 VC++ 运行库」的机器上的最后两条路。
    """

    # 名字写成大写是 PEP 8 的口径；Windows 的环境变量查表本来就大小写不敏感
    system_root = Path(os.environ.get("SYSTEMROOT") or r"C:\Windows")
    base = Path(sys.base_prefix)
    return [
        system_root / "System32" / "msvcp140.dll",
        base / "msvcp140.dll",
        base / "DLLs" / "msvcp140.dll",
    ]


def _bundled_copy() -> Path | None:
    """winrt 包目录里自带的那份 ``MSVCP140.dll``（没装 winrt 就是 ``None``）。"""

    try:  # find_spec 只找文件、不 import，windows 子模块一个都不会被拉起来
        spec = importlib.util.find_spec("winrt")
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    for location in list(spec.submodule_search_locations or ()):
        candidate = Path(location) / "msvcp140.dll"
        if candidate.is_file():
            return candidate
    if spec.origin:
        candidate = Path(spec.origin).parent / "msvcp140.dll"
        if candidate.is_file():
            return candidate
    return None


def unify_msvc_runtime() -> str:
    """把进程级的那份 ``MSVCP140`` 钉在系统版本上。**必须在 import winrt 之前调。**

    返回值只给诊断与用例用：``ok`` / ``not-windows`` / ``no-system-copy`` /
    ``failed:<原因>``。
    """

    global _DONE
    if _DONE is not None:
        return _DONE
    if sys.platform != "win32":
        _DONE = "not-windows"
        return _DONE

    for candidate in _candidates():
        if not candidate.is_file():
            continue
        try:
            # 全路径：加载的必须是这一份。同名模块已加载，后面谁再要 MSVCP140
            # 都只会拿到它，winrt 目录里那份旧的就再也进不来了。
            _PINNED.append(ctypes.WinDLL(str(candidate)))
        except OSError as exc:
            _DONE = f"failed:{exc}"
            return _DONE
        _DONE = "ok"
        return _DONE

    _DONE = "no-system-copy"
    if _bundled_copy() is not None:
        # 装了 winrt 又找不到系统运行库：这一进程有原生崩溃风险，说一声再走
        print(
            "警告：找不到系统的 MSVCP140.dll，winrt 会退回自带的 14.29 那份，"
            "之后 fastembed / onnxruntime 一加载就会把整个进程带走（0xC0000005）。"
            "装上「Microsoft Visual C++ 2015-2022 可再发行组件包」即可解决。",
            file=sys.stderr,
        )
    return _DONE


def loaded_msvcp_path() -> str | None:
    """这一秒进程里真实的 ``MSVCP140`` 是哪一份（诊断 / 用例断言用）。"""

    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 原型必须写全：不写的话 ctypes 按 c_int 收句柄，64 位的 HMODULE
        # 会随机被截成 0（本机真踩过，同一个进程里时对时错）
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetModuleFileNameW.restype = ctypes.c_uint
        kernel32.GetModuleFileNameW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
        ]
        handle = kernel32.GetModuleHandleW("msvcp140.dll")
    except OSError:  # pragma: no cover - 加载 kernel32 都失败就没什么可查的了
        return None
    if not handle:
        return None
    buffer = ctypes.create_unicode_buffer(1024)
    if not kernel32.GetModuleFileNameW(handle, buffer, 1024):
        return None
    return buffer.value or None
