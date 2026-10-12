#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把自己的 ICO（可能只有单档、单尺寸、PNG 压缩）重做成标准多尺寸 BMP 版。

为什么需要这个：
  常见的「在线转 ICO」工具会输出单档 PNG 图标（例如 122×122 PNG）。
  这种文件：
    - 没有 256 档 → 大图标视图（桌面、Alt+Tab）会模糊
    - 非标准尺寸 → Windows 缩放时插值失真
    - PNG 条目 → .NET System.Drawing.Icon 等取图路径读不到

做法：把源 ICO 里的图像解出来当普通图片，再走 image_to_ico 那套
（WIC 解码 → 高质量缩放 → 居中裁剪 → 标准尺寸打包）。

用法：
    python scripts/rebuild_ico.py <源.ico> [输出.ico]
"""
import os
import struct
import subprocess
import sys
import tempfile
import zlib

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
import image_to_ico as i2i            # noqa: E402


def entries(ico_path):
    with open(ico_path, "rb") as fh:
        d = fh.read()
    reserved, kind, n = struct.unpack("<HHH", d[:6])
    if reserved != 0 or kind != 1:
        raise ValueError("不是 ICO 文件")
    out = []
    for i in range(n):
        off = 6 + 16 * i
        w, h, colors, _z, _p, _b, size, doff = struct.unpack(
            "<BBBBHHII", d[off:off + 16])
        out.append({"w": w or 256, "h": h or 256,
                    "bytes": size, "off": doff,
                    "data": d[doff:doff + size]})
    return out


def extract_to_png(ico_path, out_png):
    """把 ICO 里最大的那一档解出来存成 PNG（PNG 条目直接复制，BMP 条目转换）"""
    es = sorted(entries(ico_path), key=lambda e: -e["w"])
    best = es[0]
    data = best["data"]
    is_png = data[:8] == b"\x89PNG\r\n\x1a\n"

    if is_png:
        with open(out_png, "wb") as fh:
            fh.write(data)
        return best["w"], best["h"], "PNG 条目"

    # 传统 BMP 条目：40 字节头 + BGRA 自下而上 + AND 掩码
    (hdr, w, h2, _pl, bpp, _cmp, _img, _x, _y, _u, _v) = \
        struct.unpack("<IiiHHIIiiII", data[:40])
    if bpp != 32:
        raise ValueError(f"只支持 32 位 BMP 条目，实际 {bpp}")
    h = h2 // 2                            # ICO 里高度字段是两倍
    body = data[hdr:hdr + w * h * 4]
    rows = [body[(h - 1 - y) * w * 4:(h - y) * w * 4] for y in range(h)]

    raw = bytearray()
    for row in rows:
        raw.append(0)
        for x in range(w):
            i = x * 4
            raw += bytes((row[i + 2], row[i + 1], row[i], row[i + 3]))

    def chunk(tag, b):
        return (struct.pack(">I", len(b)) + tag + b
                + struct.pack(">I", zlib.crc32(tag + b) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    with open(out_png, "wb") as fh:
        fh.write(png)
    return w, h, "BMP 条目"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = os.path.abspath(sys.argv[1])
    out = (os.path.abspath(sys.argv[2]) if len(sys.argv) > 2
           else i2i.DEFAULT_OUT)
    if not os.path.exists(src):
        print(f"❌ 源文件不存在: {src}")
        return 1

    print(f"源: {src}")
    print("  现有条目:")
    for e in entries(src):
        print(f"    {e['w']}×{e['h']}  {e['bytes'] / 1024:.1f} KB")

    tmp_png = os.path.join(tempfile.gettempdir(),
                           f"rebuild_{os.getpid()}.png")
    try:
        w, h, kind = extract_to_png(src, tmp_png)
        print(f"  取出最大档: {w}×{h} ({kind}) → 中间 PNG")
        print()
        print("  按标准尺寸重做:")
        sys.argv = ["image_to_ico.py", tmp_png, out]
        rc = i2i.main()
    finally:
        try:
            os.remove(tmp_png)
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
