"""晨报投递层：三通道各自可断言，一次真通知都不弹（TECH §10.3）。

投递是"最后一公里"：它失败不该让晨报本身失败，也不该静默——每个通道都回一行
回执，接不上就拿回执去查。这里用假 runner 钉住 toast、用 io.StringIO 钉住 cli、
用真文件钉住 file。
"""

from __future__ import annotations

import io
from pathlib import Path

from yixiang.gateway import sinks

DAY = "2026-09-19"
BODY = (
    "# 2026-09-19 的日报\n\n## 今日安排\n（今天没有安排）\n\n"
    "## 影视推荐\n1. 《夏日大作战》\n"
)


def test_sink_names_are_whitelisted_and_unknown_ones_drop():
    assert sinks.parse_sinks("cli,file") == ("cli", "file")
    assert sinks.parse_sinks(" CLI ,  ,tost ,file ") == ("cli", "file")
    # 空 / 全是不认识的名字 → 回落默认："配错了"不等于"不投递"
    assert sinks.parse_sinks("") == sinks.DEFAULT_SINKS
    assert sinks.parse_sinks("nope") == sinks.DEFAULT_SINKS


def test_file_sink_appends_one_block_per_delivery(tmp_path: Path):
    data_dir = tmp_path / "data"

    sinks.sink_file(BODY, data_dir=data_dir, day=DAY)
    sinks.sink_file(BODY, data_dir=data_dir, day=DAY)

    text = sinks.deliveries_path(data_dir, DAY).read_text(encoding="utf-8")
    assert text.count("## 今日安排") == 2  # 两次投递 = 两块，不互相覆盖
    assert "（今天没有安排）" in text


def test_cli_sink_writes_the_body_to_the_given_stream():
    out = io.StringIO()

    receipt = sinks.sink_cli(BODY, out=out)

    assert "已打印" in receipt
    assert "夏日大作战" in out.getvalue()


def test_toast_command_carries_the_title_and_escapes_the_body():
    command = sinks.toast_command("（补发）\n<div>今天有三件事 & 一件事</div>")

    assert "powershell" in command[0].lower()
    script = command[-1]
    assert "ToastNotification" in script
    assert "补发" in script
    # 正文是拼进 PowerShell 单引号字符串里的 XML：先转义，才不会被 < & ' 打破
    assert "&amp;" in script and "&lt;div&gt;" in script


def test_toast_failure_degrades_without_raising():
    def boom(*args, **kwargs):
        raise OSError("这台机器没有 powershell")

    receipt = sinks.sink_toast(BODY, runner=boom, platform="win32")

    assert "toast" in receipt and "没有 powershell" in receipt


def test_non_windows_skips_toast_instead_of_failing():
    receipt = sinks.sink_toast(BODY, platform="linux")

    assert "跳过" in receipt


def test_deliver_reports_every_channel_and_never_raises(tmp_path: Path):
    calls: list[list[str]] = []

    receipts = sinks.deliver(
        BODY,
        sinks=("cli", "file", "toast"),
        data_dir=tmp_path / "data",
        day=DAY,
        out=io.StringIO(),
        runner=lambda command, **kwargs: calls.append(command),
        platform="win32",
    )

    assert len(receipts) == 3
    assert len(calls) == 1
    assert sinks.deliveries_path(tmp_path / "data", DAY).is_file()


def test_deliver_keeps_going_when_one_channel_is_broken(tmp_path: Path):
    def boom(*args, **kwargs):
        raise OSError("通知服务不可用")

    receipts = sinks.deliver(
        BODY,
        sinks=("toast", "file"),
        data_dir=tmp_path / "data",
        day=DAY,
        runner=boom,
        platform="win32",
    )

    assert len(receipts) == 2
    assert "通知服务不可用" in receipts[0]
    # 前一个通道炸了，后面的 file 照投——这就是"每个通道独立"的意思
    assert sinks.deliveries_path(tmp_path / "data", DAY).is_file()
