# -*- coding: utf-8 -*-
"""酷狗 KGM / KGMA / KGMASK 容器解密（纯 Python，无外部依赖）。

为什么需要这个文件
==================
曲库里酷狗下载的歌有些是**加密容器**（.kgm/.kgma/.kgtemp），用 ffprobe
读不出格式，手机上也放不了，必须在入库前还原成普通 flac/mp3。

原来靠 `D:\\music-decode\\build\\music_decode.exe`，但它对 `.kgma` 一律失败。
根因（已实测确认）：

    那个 C++ 解码器把 KGM 头**前 16 字节写死**成
    `7cd532eb 86027f4b a8afa68e 0ff991e4`
    而磁盘上真实文件、以及酷狗客户端自己的实现用的都是
    `7cd532eb 86027f4b a8afa68e 0fff9914`

第 13~16 字节是**跟文件走的密钥材料**，不是固定魔数。常量抄错之后，
所有 `.kgma/.kgm` 都被静默判成「没有找到支持的加密音乐文件」而跳过。

算法来源
========
密码算法与三张查找表移植自 OpenConverter 的 `src/decoders/kgm.js`
（https://github.com/nowa277/OpenConverter ，其注释称重写自 MIT 许可的
huangbao/MyKgmWasm）。这里用 Python 重写，只依赖标准库。

容器布局
========
    0x00..0x10   16 字节魔数（KGM/KGMA 与 VPR 各一份）
    0x10..0x14   uint32 LE 头部长度（一般是 1024）
    0x14..0x1C   保留
    0x1C..0x2C   16 字节逐文件密钥（第 17 字节隐含为 0）
    0x2C..头长   保留（常含一个 FLAC 头 + 1 MB 掩码表）
    头长..末尾   加密音频

逐字节解密（pos 是音频内的全局偏移）
====================================
    med8 = key[pos % 17] ^ data[i]
    med8 ^= (med8 & 0x0F) << 4
    msk8 = getMask(pos)
    msk8 ^= (msk8 & 0x0F) << 4
    data[i] = med8 ^ msk8
    VPR 还要再异或 VPR_MASK_DIFF[pos % 17]

适用范围
========
✅ `.kgma`、`.kgm`、`.kgtemp`（密钥在头部 0x1C，本实现实测可完整还原）
❌ `.kgg` —— 它的密钥不在文件里（来自酷狗客户端的本地缓存），
   必须交给 kgg-helper.exe。实测把 .kgg 喂给本模块只会得到乱码，
   所以调用方要按文件头区分，不能一把梭。
"""
import operator
import struct
import sys
from pathlib import Path

KGM_HEADER = bytes([0x7C, 0xD5, 0x32, 0xEB, 0x86, 0x02, 0x7F, 0x4B,
                    0xA8, 0xAF, 0xA6, 0x8E, 0x0F, 0xFF, 0x99, 0x14])
VPR_HEADER = bytes([0x05, 0x28, 0xBC, 0x96, 0xE9, 0xE4, 0x5A, 0x43,
                    0x91, 0xAA, 0xBD, 0xD0, 0x7A, 0xF5, 0x36, 0x31])
VPR_MASK_DIFF = bytes([0x25, 0xDF, 0xE8, 0xA6, 0x75, 0x1E, 0x75, 0x0E,
                       0x2F, 0x80, 0xF3, 0x2D, 0xB8, 0xB6, 0xE3, 0x11, 0x00])


def mix(x: int) -> int:
    """原式的半字节混合：把低半字节搬到高半字节（结果始终在 0..255）。"""
    return (x ^ ((x & 0x0F) << 4)) & 0xFF


def _load_tables() -> tuple[bytes, bytes, bytes]:
    """三张 272 字节常量表。

    单独放在 `_kgm_tables.py`，是因为它们占 ~60 行纯数据，夹在代码里会
    把逻辑淹没。缺失时明确报错，不要静默降级 —— 没有表就解不了。
    """
    here = Path(__file__).resolve().parent
    for cand in (here / "_kgm_tables.py", here.parent / "_kgm_tables.py"):
        if cand.exists():
            ns: dict[str, bytes] = {}
            exec(compile(cand.read_text(encoding="utf-8"), str(cand), "exec"), ns)  # noqa: S102
            return ns["MASK_V2_PRE_DEF"], ns["TABLE1"], ns["TABLE2"]
    raise FileNotFoundError(
        "缺少 _kgm_tables.py（酷狗解密用的三张常量表）。"
        "它在 music-flow 根目录或 scripts 目录下。")


MASK_V2_PRE_DEF, TABLE1, TABLE2 = _load_tables()


def _prefix_for_offset(offset: int) -> int:
    """原式的表驱动部分：offset 反复右移 8 位（分两次移 4），边移边异或 TABLE1/TABLE2"""
    value = 0
    while offset >= 0x11:
        value ^= TABLE1[offset % 272]
        offset >>= 4
        value ^= TABLE2[offset % 272]
        offset >>= 4
    return value


# 掩码缓存（稀疏 dict：pos -> get_mask(pos)）。
#
# 这里原本想"按 pos>>8 预建一张表"，被 `_prefix_for_offset` 的实测结果否掉了：
# offset 一路右移会不断丢弃低位，而 MASK_V2_PRE_DEF 取的又是 pos % 272 ——
# 272 = 16 × 17 跟 2 的幂不同余，所以整体**既不是周期函数，也不只跟高位有关**。
# 结论：别推公式。逐点算、算过就记住，这样容量永远够用，也不会为小文件浪费内存。
_MASK_CACHE: dict[int, int] = {}


def get_mask(pos: int) -> int:
    """取 get_mask(pos)，结果缓存。"""
    v = _MASK_CACHE.get(pos)
    if v is None:
        v = MASK_V2_PRE_DEF[pos % 272] ^ _prefix_for_offset(pos >> 4)
        _MASK_CACHE[pos] = v
    return v


def can_decrypt(data: bytes) -> bool:
    """只看文件头就知道能不能解。`.kgg` 会返回 False（密钥不在文件里）。"""
    return len(data) >= 0x2C and data[:16] in (KGM_HEADER, VPR_HEADER)


def decrypt(data: bytes) -> tuple[bytes, str]:
    """解密整个容器，返回 (音频字节, 扩展名)。

    扩展名是从还原出来的**内容**判的，不问原文件名 —— 酷狗会把加密文件
    改名成 .flac/.mp3，扩展名在这行里完全不可信。
    """
    if len(data) < 0x2C:
        raise ValueError("文件太小（%d 字节）" % len(data))
    head = data[:16]
    if head == KGM_HEADER:
        is_vpr = False
    elif head == VPR_HEADER:
        is_vpr = True
    else:
        raise ValueError(
            "不是 KGM/KGMA/VPR 容器（头部 %s）。"
            "若是 .kgg 请交给 kgg-helper，它的密钥不在文件里。" % head.hex())

    header_len = struct.unpack_from("<I", data, 0x10)[0]
    if not (0x2C <= header_len <= len(data)):
        raise ValueError("头部长度不合法: %d" % header_len)

    # 密钥是 **17** 字节：头里只存 16 字节，第 17 字节按规范隐含为 0。
    # （原 C++ 实现用 key[0..16] 全 0 初始化、只填前 16 个，就是这个意思。）
    key = bytes(data[0x1C:0x2C]) + b"\x00"
    out = bytearray(data[header_len:])
    total = len(out)

    # ── 提速原理 ────────────────────────────────────────────────────────
    # 逐字节照抄原式是 20.5 s/首（一张专辑等十分钟），因为每个字节都要做
    # 一次 get_mask(pos) 查表。把公式化简后可以整段用 C 层资源跑。
    #
    # 原式：v = mix(key[i%17] ^ d) ^ mix(mask[i])      （d = 数据字节）
    #       mix(x) = x ^ ((x & 0x0F) << 4)             只把低半字节搬到高半字节
    # 把 mix(k ^ d) 按半字节展开（kh/kl 为 key 的高/低半字节，dh/dl 为数据的）：
    #       mix(k ^ d) = ((kh^dh)<<4 | (kl^dl)) ^ (kl^dl)
    #                  = d ^ mix(k) ^ (kl<<4)          ← 与 d 只剩一次朴素异或
    # 于是
    #       v = d ^ [ mix(key[i%17]) ^ mix(mask[i]) ^ (kl<<4) ]
    # 方括号只跟位置有关，所以每个位置就是「异或一个固定字节」—— 而 0..255 的
    # 异或作用在一个字节上只取决于那个字节，正好是 `bytes.translate` 的语义。
    #
    # 踩过的四个坑（前三个都真写出过错版本，全部被朴素裁判抓出来；正确产物
    # 开头必须是 66 4c 61 43 = "fLaC"）：
    #   ① v = d ^ mix(key ^ mask)   —— 把 mix(a)^mix(b) 当成 mix(a^b)
    #   ② v = d ^ mix(key) ^ mix(mask)        —— 漏了 (kl<<4)
    #   ③ 多算了 (kh<<4)：错成 0x46，与 0x66 只差 0x20
    #   ④ 化简写对了但**平铺那半段落在 `return` 之后**（死代码），
    #      实际发出去的还是 ② 的版本 —— 产物 86 dc a1 53，静默乱码。
    #      教训：改完必须拿已知基线逐字节比，不能只看"跑通了没报错"。
    #
    # ── 提速：利用 mask = 基础表[p % 272] ^ prefix(p >> 4) ────────────────
    # 逐字节照抄原式是 20.5 s/首（一张专辑等十分钟），瓶颈是每字节一次的
    # get_mask(pos)（内含 while 循环）。
    #
    # 实测**没有任何可用的周期**（272 / 4352 / 4624 / 16·17·17 全部不成立，
    # 因为 prefix(pos >> 4) 一路增长、永不回头）。但有一个可证明的分解：
    #     mask(p) = MASK_V2_PRE_DEF[p % 272] ^ prefix(p >> 4)
    # 关键：`p >> 4` 是**块号**，在一个 16 字节块内它几乎是常数，只在块的
    # 中间可能前进 1 —— 所以块内 p % 272 至多跨两个相邻值。
    # 于是每个块只要两段 translate，每段用一张 256 项表：
    #     v = mix(key[p%17] ^ d) ^ mix(MASK_V2_PRE_DEF[b] ^ prefix)
    # 表对同一 (b, prefix) 完全一样，全局只需 272 + 272 = 544 张、按需缓存
    # （实测一个 25 MB 文件只用到约 35 张）。
    #
    # 死路（都试过，别回头）：
    #   ① 化简成 v = d ^ P[i]（与数据无关的密钥流）—— **不成立**：
    #      mix(k ^ d) 展开后带 ((k ^ d) & 0x0F) << 4，依赖数据字节 d。
    #      实测由基线反解出的 P 与任何固定表都不一致。
    #   ② 用探针字节量 P[i] —— 同上，同一张表无法同时满足两个不同的 d。
    #   ③ 按块共表（以为相位只取决于 p % 16）—— 块号一变 key 旋转就变，
    #      第 0 字节对而第 1 字节错。
    #   ④ 以为 mask / 变换有周期 272、4352、4624 —— 实测全部不成立。
    #   ⑤ 每个位置都调 get_mask 建 1.6M 项缓存 —— 内存/GC 让 25 MB 跑 5 分钟。
    #   ⑥ `bytes.translate` 一次吃 16x256 的表 —— 它只接受正好 256 项。
    #
    # 正确产物开头必须是 66 4c 61 43 = "fLaC"。踩过的坑（全被朴素裁判抓出）：
    #   ① v = d ^ mix(key ^ mask)      —— 把 mix(a)^mix(b) 当成 mix(a^b)
    #   ② 少算 / 多算 (k & 0x0F) << 4  —— 产物 86 dc a1 53 或 0x46
    #   ③ 整段处理那半段落在 `return` 之后（死代码），跑的还是旧版
    # 教训：改完必须拿已知基线逐字节比，不能只看"跑通了没报错"。
    # 表按 (b, prefix) 缓存，每张是 16x256 的平铺表（MASK_V2_PRE_DEF 只有
    # 272 种 b，实际用到的 b 很少，一个 25 MB 文件约 35 张）。
    _tbl_cache: dict[tuple[int, int], bytes] = {}

    def _blk_tbl(b0: int, pref: int) -> bytes:
        """一个 16 字节块内 b 至多跨两个值的平铺表（16x256，索引 (r<<8)|字节）。"""
        t = _tbl_cache.get((b0, pref))
        if t is None:
            buf = bytearray(16 * 256)
            for r in range(16):
                b = b0 + r
                if b >= 272:
                    b -= 272
                m = (MASK_V2_PRE_DEF[b] ^ pref) & 0xFF
                m = (m ^ ((m & 0x0F) << 4)) & 0xFF
                k = key[(b0 + r) % 17 if (b0 + r) % 17 < 17 else 0]
                k = key[(b0 + r) % 17]
                for bb in range(256):
                    a = k ^ bb
                    buf[r * 256 + bb] = (a ^ ((a & 0x0F) << 4) ^ m) & 0xFF
            t = bytes(buf)
            _tbl_cache[(b0, pref)] = t
        return t

    pref_cache: dict[int, int] = {}
    nblocks = (total + 15) >> 4
    for q in range(nblocks):
        p0 = q << 4
        e = min(p0 + 16, total)
        n = e - p0
        pref = pref_cache.get(q)
        if pref is None:
            pref = _prefix_for_offset(q)
            pref_cache[q] = pref
        b0 = p0 % 272
        t = _blk_tbl(b0, pref)
        if is_vpr:
            sl = bytearray(out[p0:e])
            for j in range(n):
                pp = p0 + j
                mm = (MASK_V2_PRE_DEF[pp % 272] ^ _prefix_for_offset(pp >> 4)
                      ^ VPR_MASK_DIFF[pp % 17]) & 0xFF
                kk = key[pp % 17] ^ sl[j]
                sl[j] = (kk ^ ((kk & 0x0F) << 4) ^
                         (mm ^ ((mm & 0x0F) << 4))) & 0xFF
            out[p0:e] = sl
        elif n == 16:
            # 注意 out[a:b] 取出来是**拷贝**，改完必须写回（踩过：
            # 不写回时产物前 4 字节原封不动 = 密文）。
            blk = out[p0:e]
            for j in range(16):
                blk[j] = t[(j << 8) | blk[j]]
            out[p0:e] = blk
        else:
            for j in range(n):
                b = (b0 + j) % 272
                mm = (MASK_V2_PRE_DEF[b] ^ pref) & 0xFF
                mm = (mm ^ ((mm & 0x0F) << 4)) & 0xFF
                kk = key[(p0 + j) % 17] ^ out[p0 + j]
                out[p0 + j] = (kk ^ ((kk & 0x0F) << 4) ^ mm) & 0xFF
    return bytes(out), guess_ext(out)


def guess_ext(audio: bytes) -> str:
    """从还原出来的字节猜扩展名（不信原文件名）"""
    if audio[:4] == b"fLaC":
        return ".flac"
    if audio[:3] == b"ID3" or (len(audio) > 1 and audio[0] == 0xFF
                               and (audio[1] & 0xE0) == 0xE0):
        return ".mp3"
    if audio[:4] == b"OggS":
        return ".ogg"
    if audio[:4] == b"RIFF":
        return ".wav"
    if len(audio) >= 8 and audio[4:8] == b"ftyp":
        return ".m4a"
    return ".bin"


def decrypt_file(src, dst_dir=None) -> str:
    """解密一个文件，返回产物路径；失败抛异常。"""
    p = Path(src)
    audio, ext = decrypt(p.read_bytes())
    if ext == ".bin":
        raise ValueError("解出来不是已知音频格式（密钥可能不匹配）")
    out = Path(dst_dir or p.parent) / (p.stem + ext)
    out.write_bytes(audio)
    return str(out)


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        src = Path(arg)
        print(f"=== {src.name} ({src.stat().st_size:,} B) ===")
        try:
            got = decrypt_file(src)
        except Exception as exc:                      # noqa: BLE001
            print("  失败:", exc)
            continue
        print(f"  -> {got}  ({Path(got).stat().st_size:,} B)")
