"""图片上传的共享口径：什么算图片、怎么变成多模态的 content part。

两处要用同一套判据，否则"界面说是图、模型收到的却是坏数据"这类错最难查：

  * ``web`` 层（``web/console.py``）给上传件分类——是图就出缩略图、让用户"附上"，
    是普通文件就照旧给 ``read_file`` 的提示；
  * ``providers`` 层把本轮附图内联成 ``image_url`` 的 data URI。

判据是**文件内容**（文件头 magic bytes）而不是扩展名：一个叫 ``image.png`` 的
文本文件不是图片，硬塞进 content parts 只会让模型收到坏数据，而它对 ``read_file``
来说本来就是能读的普通文件；反过来，叫 ``截图.bin`` 的真 PNG 照样认得出来。

这里是纯函数加一次文件头读取：不认识 settings，不碰网络，也不写盘。
"""

from __future__ import annotations

import base64
from pathlib import Path

# 单张图内联上限：base64 之后体积约 +33%，8MB 的图 ≈ 11MB 的请求体。
# 超了就只发文本——消息里仍留着"（附图：…）"那一行，模型知道有这张图、在哪儿。
MAX_INLINE_IMAGE_BYTES = 8_000_000

# 判断类型只需要文件头：PNG 8 字节、JPEG 3、GIF 6、RIFF-WEBP 12，16 字节都够。
HEAD_BYTES = 16

# 认得的图片文件头。顺序无所谓——这些前缀互不为前缀。
# 故意不收 BMP：它的 magic 只有 ``BM`` 两个字节，一句以 BM 开头的普通文本就会被
# 当成图，代价（模型收到坏数据）比收益（少认一种老格式）大得多。
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# 认得出来的类型 → 扩展名。只在"对面没给扩展名"时用来补一个（QQ 图片段经常
# 只给一串 hash）：扩展名是给人看的，真类型永远由文件头说了算。
IMAGE_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def sniff_image_mime(data: bytes) -> str:
    """按文件头认图片类型；认不出返回空串（= 不是图片，走 ``read_file`` 那条路）。"""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def sniff_image_file(path: Path) -> str:
    """只读文件头判断类型：30MB 的图不必整个读进内存才知道它是不是图。"""
    try:
        with path.open("rb") as handle:
            head = handle.read(HEAD_BYTES)
    except OSError:
        return ""
    return sniff_image_mime(head)


def extension_for(mime: str) -> str:
    """``image/png`` → ``.png``；认不出的类型返回空串（那就别乱起名字）。"""
    return IMAGE_EXTENSIONS.get(mime, "")


def data_uri(data: bytes, mime: str) -> str:
    """``data:image/png;base64,…``——多模态 ``image_url`` 那一格要的正是它。"""
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


__all__ = [
    "HEAD_BYTES",
    "IMAGE_EXTENSIONS",
    "MAX_INLINE_IMAGE_BYTES",
    "data_uri",
    "extension_for",
    "sniff_image_file",
    "sniff_image_mime",
]
