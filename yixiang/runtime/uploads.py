"""上传件的落盘口径：名字怎么收敛、同名怎么让位、落在哪儿、多大算太大。

两个入口写的是同一本账：Web 控制台（用户点选的文件）与 QQ 网关（对面发来的图）。
各留一份 ``safe_name`` 的代价不是重复，而是"Web 拦住了、QQ 漏过去"这类只有出事
才发现的洞——所以上限、目录与三个函数都收在这里，两处 import 同一份。

这里是纯函数加一次写盘：不认识 settings，不碰网络。**大小上限由调用方在写之前
判**（Web 要回一句能行动的错误，QQ 要在下载途中就停下），这里只提供那把尺子。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

# 上传件的落点（``data/`` 之内，工具 read_file 的沙箱里）
UPLOADS_SUBDIR = "uploads"

# 单个上传件上限。30MB 是这么取的：够放一张手机原图 / 一份 PPT，又不至于让一个
# 2GB 的文件把内存和请求体一起打爆（图片另有一道 8MB 的"内联上限"，见
# runtime/media.py：超过它只发文本，不阻断这一轮）。
MAX_UPLOAD_BYTES = 30_000_000

# 同一条上限给人看的写法：错误文案与前端提示都从它来，避免"常量改了文案没改"
MAX_UPLOAD_LABEL = f"{MAX_UPLOAD_BYTES // 1_000_000}MB"

# 文件名收敛的尺子：认得的字符、最长多少、太长时头尾各留多少（尾巴要保住扩展名）
_UNSAFE_CHARS = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")
_NAME_LIMIT = 60
_NAME_HEAD = 44
_NAME_TAIL = 12
# 同名让位的搜索上限：200 个还撞不上说明名字本身不对劲，不必再找
_UNIQUE_LIMIT = 200


def safe_upload_name(filename: str) -> str:
    """把上传文件名收敛成"没有目录、没有路径分隔符、没有怪字符"的一段。"""
    raw = str(filename or "").replace("\\", "/").split("/")[-1].strip()
    cleaned = _UNSAFE_CHARS.sub("-", raw).strip("-_.")
    if not cleaned:
        return "upload"
    if len(cleaned) > _NAME_LIMIT:  # 保头保尾，中间省略：扩展名不能丢
        cleaned = f"{cleaned[:_NAME_HEAD]}-{cleaned[-_NAME_TAIL:]}"
    return cleaned


def unique_path(directory: Path, base: str) -> Path:
    """同名不覆盖：第二个变成 ``xxx-2.md``（上传两次同名文件不该丢第一份）。

    搜到头都没有空位就抛 ``FileExistsError``——这不是"目录满了"，是名字太怪。
    调用方各自把它翻译成自己那层的错误（Web 回 409，QQ 只记一条审计）。
    """
    candidate = directory / base
    if not candidate.exists():
        return candidate
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    for index in range(2, _UNIQUE_LIMIT):
        candidate = directory / f"{stem}-{index}{dot}{ext}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"{directory} 里同名文件太多：{base}")


def store_upload(data: bytes, *, directory: Path, filename: str, now: datetime) -> Path:
    """把一份上传件落到 ``directory``：``YYYY-MM-DD-<收敛过的原名>``，同名让位。

    日期不是装饰：上传件一律按这个名字排序，磁盘上的顺序就是时间顺序，
    界面按文件名倒序即是"新的在前"。
    """
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d")
    target = unique_path(directory, f"{stamp}-{safe_upload_name(filename)}")
    target.write_bytes(data)
    return target


__all__ = [
    "MAX_UPLOAD_BYTES",
    "MAX_UPLOAD_LABEL",
    "UPLOADS_SUBDIR",
    "safe_upload_name",
    "store_upload",
    "unique_path",
]
