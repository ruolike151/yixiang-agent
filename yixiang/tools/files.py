"""``read_file``：读 ``data/`` 下的文本文件（Web 控制台"上传文件"的读入口，§9.2）。

为什么要有这个工具：Web 控制台能上传文件，但如果模型没法读，上传就只是往
磁盘上扔垃圾。上传件与 ``data/*.md`` 一样属于**不可信外部内容**，所以出口只有
一条：``wrap_external(source="file")``（§14.3-2 的同一道防线）。

三条边界：
  * 路径先过注册表的 ``safe_path()``（``path_args=("path",)``，§9.4 第 1 层），
    本模块只负责"读得到就读"，不做第二套路径判断；
  * 二进制文件（PDF / 图片 / 压缩包）**如实拒绝**并给出可行动的替代做法，
    而不是吐一屏乱码让模型去猜；
  * 单次读取上限 2000 字（与 ``TOOL_RESULT_LIMIT`` 同档，§5.5）——超长文件
    靠"再读一次时换个 offset"这种需求不在 P0 范围里，先如实说明截断。
"""

from __future__ import annotations

from pathlib import Path

from yixiang.runtime.external import wrap_external
from yixiang.tools.registry import error_text

# 一次读多少字：默认给 1500，硬上限 2000（注册表还会再截一次，那是兜底不是口径）
DEFAULT_MAX_CHARS = 1500
MAX_CHARS = 2000
# 只看前 2KB 判断是不是二进制——足够认出 PDF（%PDF）/ PNG / zip，且不读整份文件
BINARY_SNIFF_BYTES = 2048


def read_file(data_dir: Path | str, path: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """读 ``data/`` 下的一个文本文件，返回包好的外部内容或可行动的错误文本。

    ``path`` 由注册表解析成 ``data/`` 内的绝对路径（沙箱在那边，不在这一层）。
    """
    root = Path(data_dir)
    target = Path(path)
    name = _display_name(root, target)
    limit = _clamp(max_chars)

    if target.is_dir():
        return error_text(
            "bad_type",
            "path",
            f"{name} 是一个目录；传单个文件名，或先用 yixiang memory list 看数据目录结构",
        )
    if not target.is_file():
        return error_text(
            "not_found",
            "path",
            f"data/ 下没有 {name}；先在上传面板上传，或换成已存在的文件名",
        )

    raw = target.read_bytes()
    if _looks_binary(raw):
        return error_text(
            "bad_type",
            "path",
            f"{name} 看着是二进制文件（PDF / 图片 / 压缩包），P0 只能读纯文本；"
            "先用外部工具转成 .md / .txt 再上传",
        )

    text = raw.decode("utf-8", errors="replace")
    body = text[:limit]
    dropped = len(text) - len(body)
    if dropped > 0:
        body = f"{body}\n（只读了前 {limit} 字，后面还有 {dropped} 字没进上下文）"
    return wrap_external(f"文件：data/{name}\n\n{body}", source="file")


def _display_name(root: Path, target: Path) -> str:
    """给人看的相对路径；越界或异常时退回文件名（路径校验不在这里做）。"""
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        return target.name


def _clamp(max_chars: int | str | None) -> int:
    try:
        value = int(max_chars)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS
    return max(200, min(value, MAX_CHARS))


def _looks_binary(raw: bytes) -> bool:
    """NUL 字节是最省事的文本/二进制判据（文本文件里不该出现 NUL）。"""
    return b"\x00" in raw[:BINARY_SNIFF_BYTES]


__all__ = ["DEFAULT_MAX_CHARS", "MAX_CHARS", "read_file"]
