"""晨报投递层：把一段文本送到 cli / file / toast（TECH §10.3、§10.3.1）。

三条纪律：

  1. **投递失败不抛异常**：内容已经生成、也已经落盘（``tools/brief.py``），一个
     通道接不上不该让整个 job 变 failed——**回执**里说清楚就行；
  2. **通道之间互不影响**：``toast`` 挂了，``file`` 照样写；
  3. **零依赖**：toast 走 Windows 自带的 WinRT + PowerShell；接不上就退回"正文在
     终端与流水文件里"，绝不因为一条通知给项目拖上一个 GUI 依赖。
"""

from __future__ import annotations

import html
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from yixiang.tools.brief import BRIEF_DIRNAME

SINK_CHOICES: tuple[str, ...] = ("cli", "file", "toast")
DEFAULT_SINKS: tuple[str, ...] = ("cli", "file")
# 投递流水与 daily_brief 的内容文件**分开**：一个是"生成了什么"，一个是"发出去几回"
DELIVERIES_PREFIX = "deliveries-"
_TOAST_TITLE = "以湘 · 晨报"


def parse_sinks(raw: str) -> tuple[str, ...]:
    """``"cli,file"`` → ``("cli", "file")``；不认识的名字丢掉，全丢光回落默认。"""
    picked: list[str] = []
    for item in str(raw or "").split(","):
        name = item.strip().lower()
        if name in SINK_CHOICES and name not in picked:
            picked.append(name)
    return tuple(picked) or DEFAULT_SINKS


def deliveries_path(data_dir: Path | str, day: str) -> Path:
    return Path(data_dir) / BRIEF_DIRNAME / f"{DELIVERIES_PREFIX}{day}.md"


def sink_cli(text: str, *, out: TextIO | None = None) -> str:
    stream = out if out is not None else sys.stdout
    stream.write(text.rstrip("\n") + "\n")
    stream.flush()
    return "已打印到终端"


def sink_file(text: str, *, data_dir: Path | str, day: str) -> str:
    path = deliveries_path(data_dir, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:  # 追加：一天补发两次都要看得见
        handle.write(f"## {day}\n\n{text.rstrip()}\n\n")
    return f"已追加 {path.name}"


def toast_command(text: str) -> list[str]:
    """Windows toast 的命令行（纯函数：可断言，不真的弹）。

    ``html.escape`` 默认连单引号一起转义——这不是手滑：整段 XML 是塞进 PowerShell
    的**单引号**字符串里的，正文里出现一个 ``'`` 就会把脚本切断。
    """
    body = html.escape(text.strip())
    script = (
        "$ErrorActionPreference='Stop';"
        "[void][Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime];"
        "[void][Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,"
        "ContentType=WindowsRuntime];"
        "$doc=New-Object Windows.Data.Xml.Dom.XmlDocument;"
        "$doc.LoadXml('<toast><visual><binding template=\"ToastGeneric\">"
        f"<text>{_TOAST_TITLE}</text><text>{body}</text>"
        "</binding></visual></toast>');"
        "$toast=New-Object Windows.UI.Notifications.ToastNotification $doc;"
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        f"CreateToastNotifier('{_TOAST_TITLE}').Show($toast)"
    )
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]


def sink_toast(
    text: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    platform: str = sys.platform,
) -> str:
    if platform != "win32":
        return f"非 Windows（{platform}），toast 跳过"
    try:
        runner(toast_command(text), capture_output=True, timeout=10)
    except Exception as exc:  # noqa: BLE001 - 通知接不上不是投递失败
        return f"toast 没弹出来（{exc}）：正文见终端与 {DELIVERIES_PREFIX}*.md"
    return "已弹桌面通知"


def deliver(
    text: str,
    *,
    sinks: Sequence[str],
    data_dir: Path | str,
    day: str,
    out: TextIO | None = None,
    runner: Callable[..., Any] = subprocess.run,
    platform: str = sys.platform,
) -> list[str]:
    """按顺序投递，返回每个通道一行回执；单个通道失败不影响其它通道。"""
    receipts: list[str] = []
    for name in parse_sinks(",".join(sinks)):
        try:
            if name == "cli":
                receipts.append(sink_cli(text, out=out))
            elif name == "file":
                receipts.append(sink_file(text, data_dir=data_dir, day=day))
            else:
                receipts.append(sink_toast(text, runner=runner, platform=platform))
        except Exception as exc:  # noqa: BLE001 - 回执就是给排查用的
            receipts.append(f"{name} 投递失败：{exc}")
    return receipts


__all__ = [
    "DEFAULT_SINKS",
    "DELIVERIES_PREFIX",
    "SINK_CHOICES",
    "deliver",
    "deliveries_path",
    "parse_sinks",
    "sink_cli",
    "sink_file",
    "sink_toast",
    "toast_command",
]
