#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成项目图标 music-flow.ico

不依赖 Pillow：直接构造 BMP 位图数据并包一层 ICO 头。
图标是一个深色圆角方块 + 蓝色音符（应用界面是深色 + 蓝色强调色）。
"""
import math
import os
import struct

OUT = r"D:\music-flow\assets\music-flow.ico"
SIZES = (256, 128, 64, 48, 32, 16)

# 配色（与界面一致）
BG = (14, 17, 22)          # #0e1116
PANEL = (22, 27, 34)       # 稍亮的面板
ACCENT = (76, 154, 255)    # #4c9aff
ACCENT_DIM = (47, 127, 224)
WHITE = (230, 237, 243)


def blend(c1, c2, t):
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1, c2))


def pixel(x, y, s):
    """返回 (r,g,b,a)。坐标已归一化到 0..1 便于按尺寸缩放。"""
    u = (x + 0.5) / s
    v = (y + 0.5) / s

    # 圆角方块：判断是否在圆角矩形内
    r = 0.22                                   # 圆角半径（相对）
    cx = min(max(u, r), 1 - r)
    cy = min(max(v, r), 1 - r)
    d = math.hypot(u - cx, v - cy)
    if d > r:
        return (0, 0, 0, 0)                    # 角落透明

    col = BG
    # 顶部到中部做一点渐变，显得有层次
    col = blend(BG, PANEL, max(0.0, 1.0 - v * 2.2) * 0.55)

    # ---- 音符：符头（椭圆）+ 符干（竖条）+ 符尾（斜条）----
    # 符头
    hx, hy = 0.40, 0.66
    if ((u - hx) / 0.155) ** 2 + ((v - hy) / 0.115) ** 2 <= 1.0:
        col = ACCENT
    # 符干
    if 0.525 <= u <= 0.585 and 0.30 <= v <= 0.70:
        col = ACCENT
    # 符尾（从符干顶向右下的一小段）
    if 0.545 <= u <= 0.80 and 0.28 <= v <= 0.42:
        # 一条斜带
        t = (u - 0.545) / 0.255
        band = 0.10 - 0.055 * t
        if abs(v - (0.30 + 0.075 * t)) <= band:
            col = ACCENT_DIM

    # 边缘轻描边，让深色图标在浅色桌面上也能看清
    edge = r - d
    if edge < 0.012:
        col = blend(col, WHITE, 0.30)

    return (col[0], col[1], col[2], 255)


def make_bmp(size):
    """生成一张 size×size 的 32 位 BMP（ICO 内部格式，自下而上）"""
    rows = []
    for y in range(size - 1, -1, -1):
        row = bytearray()
        for x in range(size):
            r, g, b, a = pixel(x, y, size)
            row += bytes((b, g, r, a))          # BMP 是 BGRA
        rows.append(bytes(row))
    body = b"".join(rows)

    # AND 掩码：32 位图里全 0 即可（透明度靠 alpha 通道）
    and_mask = b"\x00" * (((size + 31) // 32) * 4 * size)

    header = struct.pack("<IiiHHIIiiII",
                         40,                 # biSize
                         size, size,         # 宽高
                         1, 32,              # 平面数、位深
                         0,                  # 压缩
                         len(body) + len(and_mask),
                         2835, 2835,         # 分辨率
                         0, 0)               # 调色板
    return header + body + and_mask


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    images = [(s, make_bmp(s)) for s in SIZES]

    out = bytearray()
    # ICONDIR
    out += struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, data in images:
        w = 0 if s >= 256 else s                 # 256 用 0 表示
        h = 0 if s >= 256 else s
        out += struct.pack("<BBBBHHII",
                           w, h, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _s, data in images:
        out += data

    with open(OUT, "wb") as fh:
        fh.write(out)

    print(f"已生成: {OUT}")
    print(f"  尺寸: {', '.join(str(s) for s in SIZES)}")
    print(f"  大小: {len(out) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
