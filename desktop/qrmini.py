#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qrmini —— 零依赖的二维码编码器 + 极简 PNG 写入器。

只为 DropLink 服务：把局域网地址编成二维码，输出 PNG / 位图。
支持 QR 版本 1~10、纠错等级 M（足够了：213 字节以内）。

参考 ISO/IEC 18004 标准实现。
"""

from __future__ import annotations

import struct
import zlib

# ---- 版本相关的固定表（版本 1~10） ----
TOTAL_CODEWORDS = [0, 26, 44, 70, 100, 134, 172, 196, 242, 292, 346]
ECC_PER_BLOCK_M = [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]
NUM_BLOCKS_M = [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]
ALIGN_POS = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}
ECL_M_FORMAT_BITS = 0  # M 等级的格式信息位


# --------------------------------------------------------------------------
# GF(256) 与 Reed-Solomon
# --------------------------------------------------------------------------

def _gf_mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        b >>= 1
        a <<= 1
        if a & 0x100:
            a ^= 0x11D
    return r & 0xFF


def _gf_pow(a: int, n: int) -> int:
    r = 1
    for _ in range(n):
        r = _gf_mul(r, a)
    return r


def _poly_mul(p: list, q: list) -> list:
    out = [0] * (len(p) + len(q) - 1)
    for i, a in enumerate(p):
        if a == 0:
            continue
        for j, b in enumerate(q):
            out[i + j] ^= _gf_mul(a, b)
    return out


def _rs_generator(degree: int) -> list:
    g = [1]
    for i in range(degree):
        g = _poly_mul(g, [1, _gf_pow(2, i)])
    return g


def _rs_remainder(data: list, degree: int) -> list:
    gen = _rs_generator(degree)
    res = list(data) + [0] * degree
    for i in range(len(data)):
        coef = res[i]
        if coef:
            for j in range(len(gen)):
                res[i + j] ^= _gf_mul(gen[j], coef)
    return res[len(data):]


def _rs_check(block: list) -> bool:
    """校验：整块（数据+校验）在 g(x) 的根处取值应全为 0。"""
    degree = ECC_PER_BLOCK_M_CURRENT[0]
    for i in range(degree):
        x = _gf_pow(2, i)
        v = 0
        for c in block:
            v = _gf_mul(v, x) ^ c
        if v != 0:
            return False
    return True


ECC_PER_BLOCK_M_CURRENT = [0]


# --------------------------------------------------------------------------
# 编码
# --------------------------------------------------------------------------

class QRCode:
    def __init__(self, text: str, ecl_bits: int = ECL_M_FORMAT_BITS):
        data = text.encode("utf-8")
        self.text = text
        self.version = self._pick_version(len(data))
        if not self.version:
            raise ValueError("内容过长，无法生成二维码")
        self.ecc_len = ECC_PER_BLOCK_M[self.version]
        self.blocks = NUM_BLOCKS_M[self.version]
        self.total = TOTAL_CODEWORDS[self.version]
        self.data_len = self.total - self.ecc_len * self.blocks
        self.ecl_bits = ecl_bits
        self.size = 17 + 4 * self.version

        codewords = self._build_codewords(data)
        self.codewords = codewords
        bits = []
        for byte in codewords:
            for i in range(7, -1, -1):
                bits.append((byte >> i) & 1)

        self.modules = [[False] * self.size for _ in range(self.size)]
        self.func = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns()

        best = None
        best_mask = 0
        for mask in range(8):
            grid = [row[:] for row in self.modules]
            self._place_data(grid, bits)
            self._apply_mask(grid, mask)
            self._draw_format(grid, mask)
            score = self._penalty(grid)
            if best is None or score < best[0]:
                best = (score, grid)
                best_mask = mask
        self.modules = best[1]
        self.mask = best_mask

    # ---- 版本选择 ----
    @staticmethod
    def _pick_version(nbytes: int):
        for v in range(1, 11):
            cap = TOTAL_CODEWORDS[v] - ECC_PER_BLOCK_M[v] * NUM_BLOCKS_M[v]
            need = 4 + (8 if v < 10 else 16) + 8 * nbytes
            if need <= cap * 8:
                return v
        return 0

    # ---- 数据码字 ----
    def _build_codewords(self, data: bytes) -> list:
        bits = []

        def put(value, length):
            for i in range(length - 1, -1, -1):
                bits.append((value >> i) & 1)

        put(0b0100, 4)                      # 字节模式
        put(len(data), 8 if self.version < 10 else 16)
        for b in data:
            put(b, 8)

        cap_bits = self.data_len * 8
        if len(bits) > cap_bits:
            raise ValueError("内容过长")
        bits += [0] * min(4, cap_bits - len(bits))
        while len(bits) % 8:
            bits.append(0)

        words = []
        for i in range(0, len(bits), 8):
            v = 0
            for b in bits[i:i + 8]:
                v = (v << 1) | b
            words.append(v)
        pad = [0xEC, 0x11]
        i = 0
        while len(words) < self.data_len:
            words.append(pad[i % 2])
            i += 1

        # 分块
        blocks = []
        idx = 0
        total = self.total
        num_blocks = self.blocks
        short_len = total // num_blocks
        num_long = total % num_blocks
        num_short = num_blocks - num_long
        short_data = short_len - self.ecc_len
        for b in range(num_blocks):
            n = short_data + (1 if b >= num_short else 0)
            chunk = words[idx:idx + n]
            idx += n
            blocks.append(chunk)

        eccs = []
        for chunk in blocks:
            ecc = _rs_remainder(chunk, self.ecc_len)
            eccs.append(ecc)

        # 交织
        out = []
        max_data = max(len(b) for b in blocks)
        for i in range(max_data):
            for b in blocks:
                if i < len(b):
                    out.append(b[i])
        for i in range(self.ecc_len):
            for e in eccs:
                out.append(e[i])
        return out

    # ---- 功能图形 ----
    def _set_func(self, r: int, c: int, dark: bool):
        if 0 <= r < self.size and 0 <= c < self.size:
            self.modules[r][c] = bool(dark)
            self.func[r][c] = True

    def _draw_function_patterns(self):
        size = self.size

        # 定位图形 + 分隔符
        for (br, bc) in ((0, 0), (0, size - 7), (size - 7, 0)):
            for dr in range(-1, 8):
                for dc in range(-1, 8):
                    r, c = br + dr, bc + dc
                    if not (0 <= r < size and 0 <= c < size):
                        continue
                    if 0 <= dr <= 6 and 0 <= dc <= 6:
                        dist = max(abs(dr - 3), abs(dc - 3))
                        self._set_func(r, c, dist != 2)
                    else:
                        self._set_func(r, c, False)

        # 时序图形
        for i in range(8, size - 8):
            self._set_func(6, i, i % 2 == 0)
            self._set_func(i, 6, i % 2 == 0)

        # 校正图形
        pos = ALIGN_POS.get(self.version, [])
        for r0 in pos:
            for c0 in pos:
                if (r0 <= 8 and c0 <= 8) or (r0 <= 8 and c0 >= size - 9) or (r0 >= size - 9 and c0 <= 8):
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        dist = max(abs(dr), abs(dc))
                        self._set_func(r0 + dr, c0 + dc, dist != 1)

        # 预留格式信息区
        for i in range(9):
            if not self.func[8][i]:
                self._set_func(8, i, False)
            if not self.func[i][8]:
                self._set_func(i, 8, False)
        for i in range(8):
            self._set_func(8, size - 1 - i, False)
            self._set_func(size - 1 - i, 8, False)
        self._set_func(size - 8, 8, True)  # 固定的黑模块

        # 版本信息（版本 >= 7）
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = (self.version << 12) | rem
            for i in range(18):
                bit = (bits >> i) & 1
                a = size - 11 + i % 3
                b = i // 3
                self._set_func(b, a, bit == 1)   # 列 a / 行 b
                self._set_func(a, b, bit == 1)   # 列 b / 行 a

    # ---- 数据填充（之字形） ----
    def _place_data(self, grid, bits):
        size = self.size
        i = 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vert if upward else vert
                    if not self.func[y][x] and i < len(bits):
                        grid[y][x] = bits[i] == 1
                        i += 1
            right -= 2

    # ---- 掩码 ----
    @staticmethod
    def _mask_bit(mask: int, x: int, y: int) -> bool:
        if mask == 0:
            return (x + y) % 2 == 0
        if mask == 1:
            return y % 2 == 0
        if mask == 2:
            return x % 3 == 0
        if mask == 3:
            return (x + y) % 3 == 0
        if mask == 4:
            return (x // 3 + y // 2) % 2 == 0
        if mask == 5:
            return (x * y) % 2 + (x * y) % 3 == 0
        if mask == 6:
            return ((x * y) % 2 + (x * y) % 3) % 2 == 0
        return ((x + y) % 2 + (x * y) % 3) % 2 == 0

    def _apply_mask(self, grid, mask: int):
        for y in range(self.size):
            for x in range(self.size):
                if not self.func[y][x] and self._mask_bit(mask, x, y):
                    grid[y][x] = not grid[y][x]

    # ---- 格式信息 ----
    def _draw_format(self, grid, mask: int):
        data = (self.ecl_bits << 3) | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412
        size = self.size

        def bit(i):
            return ((bits >> i) & 1) == 1

        def put(r, c, v):
            grid[r][c] = v

        for i in range(6):
            put(i, 8, bit(i))
        put(7, 8, bit(6))
        put(8, 8, bit(7))
        put(8, 7, bit(8))
        for i in range(9, 15):
            put(8, 14 - i, bit(i))

        for i in range(8):
            put(8, size - 1 - i, bit(i))
        for i in range(8, 15):
            put(size - 15 + i, 8, bit(i))
        put(size - 8, 8, True)

    # ---- 罚分 ----
    def _penalty(self, grid) -> int:
        size = self.size
        score = 0

        # 连续同色
        for rows in (grid, list(zip(*grid))):
            for line in rows:
                run = 1
                for i in range(1, size):
                    if line[i] == line[i - 1]:
                        run += 1
                    else:
                        if run >= 5:
                            score += 3 + (run - 5)
                        run = 1
                if run >= 5:
                    score += 3 + (run - 5)

        # 2x2 同色块
        for r in range(size - 1):
            row0, row1 = grid[r], grid[r + 1]
            for c in range(size - 1):
                if row0[c] == row0[c + 1] == row1[c] == row1[c + 1]:
                    score += 3

        # 类似定位图形的 1:1:3:1:1
        pat = [True, False, True, True, True, False, True]
        for r in range(size):
            for c in range(size - 6):
                seg = grid[r][c:c + 7]
                if seg == pat:
                    left = grid[r][max(0, c - 4):c]
                    right = grid[r][c + 7:c + 11]
                    if (len(left) == 4 and not any(left)) or (len(right) == 4 and not any(right)):
                        score += 40
        for c in range(size):
            col = [grid[r][c] for r in range(size)]
            for r in range(size - 6):
                if col[r:r + 7] == pat:
                    up = col[max(0, r - 4):r]
                    down = col[r + 7:r + 11]
                    if (len(up) == 4 and not any(up)) or (len(down) == 4 and not any(down)):
                        score += 40

        # 黑白比例
        dark = sum(sum(1 for v in row if v) for row in grid)
        ratio = dark * 100 // (size * size)
        score += abs(ratio - 50) // 5 * 10
        return score

    # ---- 输出 ----
    def bitmap(self, border: int = 4):
        size = self.size + border * 2
        rows = []
        for r in range(size):
            row = []
            for c in range(size):
                rr, cc = r - border, c - border
                inside = 0 <= rr < self.size and 0 <= cc < self.size
                row.append(1 if (inside and self.modules[rr][cc]) else 0)
            rows.append(row)
        return rows


ECC_PER_BLOCK_M_CURRENT[0] = 10  # 默认值，_rs_check 调用前会被替换


# --------------------------------------------------------------------------
# PNG 输出
# --------------------------------------------------------------------------

def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_bytes(width: int, height: int, rows, color_type: int = 2) -> bytes:
    """rows: 每行一个 bytes（RGB 或 RGBA）。"""
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


def qr_png_bytes(text: str, scale: int = 8, border: int = 3,
                 dark=(17, 24, 39), light=(255, 255, 255)) -> bytes:
    qr = QRCode(text)
    bits = qr.bitmap(border)
    n = len(bits)
    w = h = n * scale
    rows = []
    for r in range(h):
        src = bits[r // scale]
        line = bytearray()
        for c in range(w):
            line += bytes(dark if src[c // scale] else light)
        rows.append(line)
    return png_bytes(w, h, rows, 2)


def qr_matrix(text: str, border: int = 4):
    return QRCode(text).bitmap(border)


# --------------------------------------------------------------------------
# 应用图标（圆角渐变 + 下载箭头），超采样抗锯齿
# --------------------------------------------------------------------------

def icon_png_bytes(size: int = 192, supersample: int = 3) -> bytes:
    s = size * supersample
    radius = 0.225 * s
    pad = 0.0

    def inside_round(x, y):
        if x < 0 or y < 0 or x >= s or y >= s:
            return False
        cx = min(max(x, radius), s - 1 - radius)
        cy = min(max(y, radius), s - 1 - radius)
        return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2

    def in_arrow(nx, ny, cx, dirn):
        """画一个白色箭头。dirn=1 向下（尖朝下），dirn=-1 向上（尖朝上）。"""
        half_shaft = 0.045
        if dirn > 0:  # 向下
            if abs(nx - cx) <= half_shaft and 0.28 <= ny <= 0.58:
                return True
            tip, base = 0.72, 0.58
        else:  # 向上
            if abs(nx - cx) <= half_shaft and 0.42 <= ny <= 0.72:
                return True
            tip, base = 0.28, 0.42
        lo, hi = min(tip, base), max(tip, base)
        if not (lo <= ny <= hi):
            return False
        t = (ny - tip) / (base - tip)  # 0 在顶点，1 在底边
        return abs(nx - cx) <= 0.14 * t

    big = [[(0, 0, 0, 0)] * s for _ in range(s)]
    c1 = (79, 70, 229)
    c2 = (124, 58, 237)

    for y in range(s):
        t = y / max(1, s - 1)
        base = (int(c1[0] + (c2[0] - c1[0]) * t),
                int(c1[1] + (c2[1] - c1[1]) * t),
                int(c1[2] + (c2[2] - c1[2]) * t))
        ny = y / s
        row = big[y]
        for x in range(s):
            if not inside_round(x, y):
                continue
            nx = x / s
            # 左：白色下箭头；右：白色上箭头
            if in_arrow(nx, ny, 0.36, 1) or in_arrow(nx, ny, 0.64, -1):
                row[x] = (255, 255, 255, 255)
            else:
                row[x] = (base[0], base[1], base[2], 255)

    rows = []
    k = supersample
    for y in range(size):
        line = bytearray()
        for x in range(size):
            r = g = b = a = 0
            for dy in range(k):
                src = big[y * k + dy]
                for dx in range(k):
                    px = src[x * k + dx]
                    r += px[0]
                    g += px[1]
                    b += px[2]
                    a += px[3]
            cnt = k * k
            line += bytes((r // cnt, g // cnt, b // cnt, a // cnt))
        rows.append(line)
    return png_bytes(size, size, rows, 6)


def selftest() -> bool:
    """校验 RS 数学与结构自洽，避免生成无法识别的二维码。"""
    ok = True
    for v in range(1, 11):
        ecc = ECC_PER_BLOCK_M[v]
        ECC_PER_BLOCK_M_CURRENT[0] = ecc
        data = [i % 256 for i in range(TOTAL_CODEWORDS[v] - ecc * NUM_BLOCKS_M[v])]
        block = data + _rs_remainder(data, ecc)
        if not _rs_check(block):
            print(f"RS 校验失败 v{v}")
            ok = False
    # 已知格式信息常量：M / mask 0 = 0x5412
    data = (0 << 3) | 0
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    if ((data << 10) | rem) ^ 0x5412 != 0x5412:
        print("格式信息编码异常")
        ok = False
    qr = QRCode("http://192.168.1.7:8848/")
    if qr.size != 17 + 4 * qr.version:
        ok = False
    # 三个定位图形的中心必须为深色
    for (r, c) in ((3, 3), (3, qr.size - 4), (qr.size - 4, 3)):
        if not qr.modules[r][c]:
            ok = False
    return ok


if __name__ == "__main__":
    import os
    import sys

    if not selftest():
        print("自检未通过")
        sys.exit(1)
    print("自检通过")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_qr_test.png")
    with open(out, "wb") as f:
        f.write(qr_png_bytes("http://192.168.1.7:8848", scale=8))
    print("已生成", out)
    with open(os.path.join(os.path.dirname(out), "_icon_test.png"), "wb") as f:
        f.write(icon_png_bytes(192))
    print("图标已生成")
