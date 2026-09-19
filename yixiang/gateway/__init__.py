"""Gateway 层：协议转换与文本搬运（ADR-3）。QQ 归 P2，本部分只有 CLI。"""

from yixiang.gateway.cli import ChatCLI, CliObserver

__all__ = ["ChatCLI", "CliObserver"]
