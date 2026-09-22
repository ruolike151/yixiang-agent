"""Gateway 层：协议转换与文本搬运（ADR-3）。CLI、QQ（OneBot v11）与晨报投递层。"""

from yixiang.gateway.cli import ChatCLI, CliObserver
from yixiang.gateway.qq import QQGateway
from yixiang.gateway.sinks import DEFAULT_SINKS, deliver, parse_sinks

__all__ = [
    "DEFAULT_SINKS",
    "ChatCLI",
    "CliObserver",
    "QQGateway",
    "deliver",
    "parse_sinks",
]
