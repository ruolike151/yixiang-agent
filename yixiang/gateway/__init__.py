"""Gateway 层：协议转换与文本搬运（ADR-3）。本部分只有 CLI 与晨报投递层。"""

from yixiang.gateway.cli import ChatCLI, CliObserver
from yixiang.gateway.sinks import DEFAULT_SINKS, deliver, parse_sinks

__all__ = ["DEFAULT_SINKS", "ChatCLI", "CliObserver", "deliver", "parse_sinks"]
