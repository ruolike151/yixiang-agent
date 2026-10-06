"""QQ QRC 逐字歌词：解密 + 解析（Task 7）。

密文（base64）→ 3DES-ECB 解密 → 裸 deflate →
``[start,duration]()字(start,duration)…`` 纯函数、无网络、无第三方依赖。

密钥与方向（与上游 ``qrcDecrypt.ts`` 同名同义）：

- 24 字节密钥 ``!@#)(*$%123ZXC!@!@#)(NHL``，按 8 字节切三段 ``K1 K2 K3``；
- 解密方向 ``D K3 → E K2 → D K1``（标准 3DES-EDE，即上游 ``tripledes_key_setup(key, 0)``）；
- 8 字节分块，末块不足补零，明文按原始长度截断（**不是** PKCS#7，别去抠填充）。

唯一的坑：上游 TS 往 DES 里塞块前做过一次字节重排（``bitnum()`` 那段，等价于按 32 位
小端重读）。先按标准写；真机拿到真实 QRC 若解不出可读明文，就只改 ``_des_block()``
入口/出口那两行（8 字节按 ``[3,2,1,0,7,6,5,4]`` 换一遍），别的地方不用碰。
"""

from __future__ import annotations

import base64
import re
import zlib

from yixiang.media.lyrics import LyricLine

# 上游同款：24 字节。hex 21402329282a24253132335a5843214021402329284e484c
QRC_KEY = b"!@#)(*$%123ZXC!@!@#)(NHL"
BLOCK = 8

_LINE_HEAD = re.compile(r"^\[(\d+),(\d+)\]")
# 一个 token 是"一段非空白、非括号的文本 + 紧跟的 (start,duration)"。
# 字符类里必须把 ``)`` 也排除掉：行首的 ``()`` 空段（用例里的 ``[1234,5678]()``）否则
# 会被"从 ``)`` 起匹配"吃进第一个 token，把首字前面带上一个 ``)``。
_WORD = re.compile(r"([^\s()]+)\((\d+),(\d+)\)")


class QrcError(ValueError):
    """QRC 解不开：base64 坏了 / 长度不是 8 的整数倍 / 解出来不是裸 deflate。"""


def qrc_decrypt(cipher_b64: str) -> str:
    """base64 密文 → QRC 明文（一行一句，行内带逐字时间戳）。"""

    try:
        raw = base64.b64decode(cipher_b64, validate=False)
    except ValueError as exc:  # binascii.Error 是 ValueError 的子类
        raise QrcError(f"base64 坏了：{exc}") from exc
    if not raw or len(raw) % BLOCK:
        raise QrcError(f"密文长度 {len(raw)} 不是 {BLOCK} 的整数倍")
    blocks = (raw[i : i + BLOCK] for i in range(0, len(raw), BLOCK))
    return _inflate(b"".join(_triple_decrypt(block) for block in blocks)).decode(
        "utf-8", "replace"
    )


def qrc_encrypt(text: str) -> str:
    """``qrc_decrypt`` 的逆——**只给用例自产 round-trip 向量**，生产路径不用。"""

    plain = zlib.compress(text.encode("utf-8"))
    deflated = plain[2:-4]  # 摘掉 zlib 头尾，就是裸 deflate（解密侧走 -15 窗口）
    padded = deflated + b"\x00" * (-len(deflated) % BLOCK)
    blocks = (padded[i : i + BLOCK] for i in range(0, len(padded), BLOCK))
    return base64.b64encode(
        b"".join(_triple_encrypt(block) for block in blocks)
    ).decode("ascii")


def parse_qrc(plain: str) -> tuple[tuple[LyricLine, ...], str]:
    """QRC 明文 → ``(行, 原始逐字串)``；原始串原样回给 ``karaoke_lyric``。

    行首方括号里的 start 是**绝对**毫秒；每个字圆括号里的 s 是**相对行首**的毫秒
    ——``LyricLine.words`` 存的就是这个相对偏移（task-01 预留的那个 tuple）。
    """

    lines: list[LyricLine] = []
    for raw in plain.splitlines():
        head = _LINE_HEAD.match(raw)
        if head is None:
            continue
        matches = list(_WORD.finditer(raw, head.end()))
        text = "".join(match.group(1) for match in matches)
        if not text.strip():
            continue
        words = tuple((int(match.group(2)), match.group(1)) for match in matches)
        lines.append(LyricLine(time_ms=int(head.group(1)), text=text, words=words))
    lines.sort(key=lambda line: line.time_ms)
    return tuple(lines), plain


def _inflate(payload: bytes) -> bytes:
    engine = zlib.decompressobj(-15)  # -15 = 裸 deflate，不带 zlib 头
    try:
        return engine.decompress(payload) + engine.flush()
    except zlib.error as exc:
        raise QrcError(f"不是裸 deflate：{exc}") from exc


def _triple_decrypt(block: bytes) -> bytes:
    """D K3 → E K2 → D K1（上游 ``tripledes_key_setup(key, 0)`` 的顺序）。"""

    step = _des_block(block, QRC_KEY[16:24], decrypt=True)
    step = _des_block(step, QRC_KEY[8:16], decrypt=False)
    return _des_block(step, QRC_KEY[0:8], decrypt=True)


def _triple_encrypt(block: bytes) -> bytes:
    """逆方向：E K1 → D K2 → E K3（只给 ``qrc_encrypt`` 用）。"""

    step = _des_block(block, QRC_KEY[0:8], decrypt=False)
    step = _des_block(step, QRC_KEY[8:16], decrypt=True)
    return _des_block(step, QRC_KEY[16:24], decrypt=False)


# ---- DES（FIPS 46-3）：IP → 16 轮 → FP。表照抄标准实现，不改。
_IP = (
    58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
    62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7,
)

_FP = (
    40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
    38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
    36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25,
)

_E = (
    32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13,
    12, 13, 14, 15, 16, 17, 16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25,
    24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1,
)

_P = (
    16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
    2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25,
)

_PC1 = (
    57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18,
    10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22,
    14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4,
)

_PC2 = (
    14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10,
    23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
    41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48,
    44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32,
)

# 每轮的循环左移位数。
_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)

_S_BOXES = (
    (
        14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
        0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
        4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
        15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13,
    ),
    (
        15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
        3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
        0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
        13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9,
    ),
    (
        10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
        13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
        13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
        1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12,
    ),
    (
        7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
        13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
        10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
        3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14,
    ),
    (
        2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
        14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
        4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
        11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3,
    ),
    (
        12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
        10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
        9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
        4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13,
    ),
    (
        4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
        13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
        1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
        6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12,
    ),
    (
        13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
        1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
        7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
        2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11,
    ),
)


def _des_block(block: bytes, key: bytes, *, decrypt: bool) -> bytes:
    """单块 DES（FIPS 46-3）：IP → 16 轮 → FP。``table`` 都是 1 起始的下标。"""

    subkeys = _key_schedule(key)
    if decrypt:
        subkeys = subkeys[::-1]
    bits = _permute(_bytes_to_bits(block), _IP)
    left, right = bits[:32], bits[32:]
    for subkey in subkeys:
        left, right = right, [
            a ^ b for a, b in zip(left, _f(right, subkey), strict=True)
        ]
    return _bits_to_bytes(_permute(right + left, _FP))


def _permute(bits: list[int], table: tuple[int, ...]) -> list[int]:
    return [bits[index - 1] for index in table]


def _bytes_to_bits(block: bytes) -> list[int]:
    return [(byte >> shift) & 1 for byte in block for shift in range(7, -1, -1)]


def _bits_to_bytes(bits: list[int]) -> bytes:
    out = bytearray()
    for start in range(0, len(bits), 8):
        value = 0
        for bit in bits[start : start + 8]:
            value = (value << 1) | bit
        out.append(value)
    return bytes(out)


def _key_schedule(key: bytes) -> list[list[int]]:
    """PC1 → 左右各 28 位 → 16 轮循环左移 + PC2，得到 16 个 48 位子密钥。"""

    bits = _permute(_bytes_to_bits(key), _PC1)
    left, right = bits[:28], bits[28:]
    subkeys: list[list[int]] = []
    for shift in _SHIFTS:
        left = left[shift:] + left[:shift]
        right = right[shift:] + right[:shift]
        subkeys.append(_permute(left + right, _PC2))
    return subkeys


def _f(right: list[int], subkey: list[int]) -> list[int]:
    """轮函数：E 扩展 → 与子密钥异或 → 8 个 S 盒 → P 置换。"""

    expanded = _permute(right, _E)
    mixed = [a ^ b for a, b in zip(expanded, subkey, strict=True)]
    out: list[int] = []
    for box in range(8):
        chunk = mixed[box * 6 : box * 6 + 6]
        row = (chunk[0] << 1) | chunk[5]
        col = (chunk[1] << 3) | (chunk[2] << 2) | (chunk[3] << 1) | chunk[4]
        value = _S_BOXES[box][row * 16 + col]
        out.extend([(value >> 3) & 1, (value >> 2) & 1, (value >> 1) & 1, value & 1])
    return _permute(out, _P)
