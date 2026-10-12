#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把一张图片转成 Windows 图标 assets/music-flow.ico

实现方式（不依赖 Pillow）
------------------------
1. 解码：交给 Windows 自带的 WIC（Windows Imaging Component）。
   PowerShell 里用 System.Windows.Media.Imaging 读图 —— WebP/PNG/JPEG 都能读，
   不用自己写 VP8 解码器。
2. 缩放：WIC 的 TransformedBitmap 高质量缩放，等比缩到短边贴合后居中裁剪，
   保证不变形。
3. 打包 ICO：手写 ICONDIR + 各尺寸位图。
   - 256×256 用 **PNG** 压缩存（ICO 规范从 Vista 起支持），否则单这一张
     未压缩 BMP 就要 262 KB。
   - 小尺寸用传统 32 位 BMP（兼容性最好，且本来就只有几 KB）。
   两种都不依赖任何图像库 —— PNG 的 zlib 是标准库，BMP 是手拼字节。

用法：
    python scripts/image_to_ico.py <源图> [输出.ico]
"""
import os
import struct
import subprocess
import sys
import tempfile
import zlib

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(BASE, "assets", "music-flow.ico")
SIZES = (256, 128, 64, 48, 32, 16)
# 是否用 PNG 压缩大尺寸。设成 999 = 全部用未压缩 BMP。
#
# 实测：.NET 的 System.Drawing.Icon **不认** PNG 条目 —— 请求 256 只会
# 返回 128。为了任何取图路径（旧 .NET、第三方工具、老系统）都能拿到全部
# 尺寸，这里选择全部用 BMP。代价是文件大些（约 361 KB），但图标只存一份，
# 兼容性更重要。
PNG_MAX = 999


def decode_to_bgra(src, size):
    """用 WIC 解码 + 等比缩放 + 居中裁剪，返回 size×size 的 BGRA 字节"""
    tmp = os.path.join(tempfile.gettempdir(),
                       f"mf_ico_{size}_{os.getpid()}.png")
    ps = f"""
Add-Type -AssemblyName PresentationCore | Out-Null
$src = [System.Windows.Media.Imaging.BitmapFrame]::Create([Uri]::new('{src}'))
$w = $src.PixelWidth; $h = $src.PixelHeight
$scale = [Math]::Max({size} / $w, {size} / $h)
$nw = [int][Math]::Round($w * $scale); $nh = [int][Math]::Round($h * $scale)

$vis = New-Object System.Windows.Media.Imaging.TransformedBitmap
$vis.BeginInit()
$vis.Source = $src
$vis.Transform = (New-Object System.Windows.Media.ScaleTransform($scale, $scale))
$vis.EndInit()

$conv = New-Object System.Windows.Media.Imaging.FormatConvertedBitmap
$conv.BeginInit()
$conv.Source = $vis
$conv.DestinationFormat = [System.Windows.Media.PixelFormats]::Bgra32
$conv.EndInit()

$crop = New-Object System.Windows.Media.Imaging.CroppedBitmap
$crop.BeginInit()
$crop.Source = $conv
$crop.SourceRect = New-Object System.Windows.Int32Rect(
    [int](($nw - {size}) / 2), [int](($nh - {size}) / 2), {size}, {size})
$crop.EndInit()

$enc = New-Object System.Windows.Media.Imaging.PngBitmapEncoder
$enc.Frames.Add([System.Windows.Media.Imaging.BitmapFrame]::Create($crop))
$fs = [System.IO.File]::Create('{tmp}')
$enc.Save($fs); $fs.Close()
Write-Output 'OK'
"""
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if "OK" not in (r.stdout or ""):
        raise RuntimeError(f"WIC 解码失败: {(r.stderr or r.stdout or '')[:300]}")
    try:
        return read_png_bgra(tmp, size)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def read_png_bgra(path, size):
    """读 8 位 RGBA 的 PNG，返回 BGRA 字节（自上而下）。处理全部 5 种行滤波。"""
    with open(path, "rb") as fh:
        data = fh.read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"

    pos, idat, hdr = 8, b"", None
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + ln

    w, h, depth, ctype, _c, _f, interlace = hdr
    assert depth == 8 and ctype == 6, \
        f"只支持 8 位 RGBA，实际 depth={depth} ctype={ctype}"
    assert interlace == 0, "不支持隔行 PNG"

    raw = zlib.decompress(idat)
    bpp, stride = 4, w * 4
    out = bytearray(stride * h)
    prev = bytearray(stride)
    p = 0
    for y in range(h):
        f = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return bytes(out)


def png_bytes(bgra, size):
    """把 BGRA 打包成 8 位 RGBA 的 PNG（无滤波，压缩率够用）"""
    raw = bytearray()
    for y in range(size):
        raw.append(0)                              # filter type 0
        row = bgra[y * size * 4:(y + 1) * size * 4]
        for x in range(size):
            i = x * 4
            raw += bytes((row[i + 2], row[i + 1], row[i], row[i + 3]))  # BGRA→RGBA

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def bmp_bytes(bgra, size):
    """ICO 内的 BMP：BITMAPINFOHEADER + BGRA 自下而上 + AND 掩码"""
    rows = [bgra[y * size * 4:(y + 1) * size * 4]
            for y in range(size - 1, -1, -1)]      # BIMP 自下而上
    body = b"".join(rows)
    and_mask = b"\x00" * (((size + 31) // 32) * 4 * size)
    header = struct.pack("<IiiHHIIiiII",
                         40, size, size * 2,       # 高度字段写两倍（ICO 惯例）
                         1, 32, 0, len(body) + len(and_mask),
                         2835, 2835, 0, 0)
    return header + body + and_mask


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = os.path.abspath(sys.argv[1])
    out = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    if not os.path.exists(src):
        print(f"❌ 源图不存在: {src}")
        return 1

    print(f"源图: {src}")
    print(f"输出: {out}")
    print()

    images = []
    for s in SIZES:
        print(f"  {s:>3}×{s:<3} ", end="", flush=True)
        try:
            bgra = decode_to_bgra(src, s)
            if s >= PNG_MAX:
                data = png_bytes(bgra, s)
                kind = "PNG"
            else:
                data = bmp_bytes(bgra, s)
                kind = "BMP"
            images.append((s, data))
            print(f"✅ {kind}  {len(data) / 1024:>7.1f} KB")
        except Exception as exc:                   # noqa: BLE001
            print(f"❌ {exc}")
            return 1

    buf = bytearray()
    buf += struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, d in images:
        buf += struct.pack("<BBBBHHII",
                           0 if s >= 256 else s,
                           0 if s >= 256 else s,
                           0, 0, 1, 32, len(d), offset)
        offset += len(d)
    for _s, d in images:
        buf += d

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as fh:
        fh.write(buf)

    print()
    print(f"✅ 已生成 {out}  ({len(buf) / 1024:.1f} KB, {len(images)} 个尺寸)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
