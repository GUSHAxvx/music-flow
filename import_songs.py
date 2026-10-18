#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手动导入歌曲

流程（与自动下载的曲目走同一套后续）：
    选文件/文件夹 → 校验格式 → 拷进同步文件夹 → 按文件名打标签 → 触发同步扫描

设计要点
--------
* 用 Windows 原生对话框取路径。浏览器的 <input type="file"> 出于安全
  不会给出真实磁盘路径，只能上传副本 —— 而我们需要的正是真实路径。
* **同名去重按实测规格择优**，与自动流程一致：宁可保留规格高的那份。
* 打标签复用 tag_files.py 的实现（它按**文件头魔数**判断容器格式，
  不信扩展名 —— 大量文件是「伪装成 .mp3 的 FLAC」，按扩展名写会损坏文件）。
* 打开「接受有损」时低品质文件也允许导入；关闭时只允许无损及以上。
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.request
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
PICKER = os.path.join(BASE, "scripts", "pick_files.ps1")

# 能导入的音频扩展名
AUDIO_EXT = (".flac", ".mp3", ".m4a", ".ogg", ".wav", ".ape", ".wma", ".aac")
# 无损容器（按扩展名初判，最终以 ffprobe 实测为准）
LOSSLESS_EXT = (".flac", ".wav", ".ape", ".alac")
# 加密音乐容器（网易云 ncm / QQ音乐 qmc / 酷狗 kgg·kgma·kgm / 酷我 vpr）。
# 这些不是音频，是**加了密的容器**：必须先解码才能读规格、才能打标签、
# 手机上的播放器也才认。所以导入流程里它们要先过一遍解码器。
# 清单与解码器保持一致；另外还有一种「扩展名写成 .flac、内容却是加密的」
# （酷狗干的），靠文件头识别，见 encrypted_files()。
ENCRYPTED_EXT = (".ncm", ".qmc0", ".qmc3", ".qmcflac", ".qmcogg",
                 ".kgm", ".kgma", ".kgg", ".kgtemp", ".vpr")
# 酷狗/酷我加密文件的文件头。
#
# 为什么只比前 12 字节（踩过的坑）
# ---------------------------------
# 解码器源码里的 KGM 头是完整 16 字节，末 4 字节写死 `0f f9 91 e4`。
# 但实测磁盘上的文件是 `0f ff 99 14`（35 个样本 35 个如此），源码那个
# 常量一个都没对上 —— 说明这 4 字节是**跟文件走的数据**，不是固定魔数。
# 照抄 16 字节的话，所有变体都会被判成「不是加密文件」而静默跳过，
# 用户导 .kgma 失败就是这么来的。所以只认真正稳定的前 12 字节。
_ENC_HEADERS = (
    bytes([0x7c, 0xd5, 0x32, 0xeb, 0x86, 0x02, 0x7f, 0x4b,
           0xa8, 0xaf, 0xa6, 0x8e]),                              # 酷狗 KGM 族
    bytes([0x05, 0x28, 0xbc, 0x96, 0xe9, 0xe4, 0x5a, 0x43,
           0x91, 0xaa, 0xbd, 0xd0]),                              # 酷我 VPR
)
# 酷狗这一族的扩展名 —— 它们内容同源，解码时可以互相借道，见
# _decode_via_kgg()。
KGM_FAMILY_EXT = (".kgg", ".kgma", ".kgm", ".kgtemp")

# 纯 Python 的酷狗解密模块（scripts/kugou_kgm.py）。它按「头部 0x1C 起的
# 16 字节逐文件密钥」真解，能还原外置解码器认不出的 .kgma/.kgm/.kgtemp。
_KGM_MOD = None


def _kgm_module():
    """懒加载 scripts/kugou_kgm.py；不可用返回 None（不抛异常）。"""
    global _KGM_MOD
    if _KGM_MOD is None:
        try:
            if os.path.join(BASE, "scripts") not in sys.path:
                sys.path.insert(0, os.path.join(BASE, "scripts"))
            import kugou_kgm as _m            # noqa: N816
            _KGM_MOD = _m
        except Exception:                      # noqa: BLE001
            _KGM_MOD = False
    return _KGM_MOD or None


def _decode_via_python(src, outdir):
    """用内置酷狗解密模块解一次，成功返回产物路径，失败返回 None。

    只吃 .kgma/.kgm/.kgtemp（密钥随文件走）。.kgg 的密钥在酷狗客户端本地
    缓存里，本模块拿不到，can_decrypt() 会挡掉，仍交给 kgg-helper。
    """
    mod = _kgm_module()
    if mod is None:
        return None
    try:
        with open(src, "rb") as fh:
            head = fh.read(64)
    except OSError:
        return None
    if not mod.can_decrypt(head):
        return None
    try:
        return mod.decrypt_file(src, outdir)
    except Exception:                          # noqa: BLE001
        return None


def _starts_kgm(p):
    """文件头是不是酷狗那一族（不管它叫什么名字）"""
    try:
        with open(p, "rb") as fh:
            return fh.read(12).startswith(_ENC_HEADERS[0])
    except OSError:
        return False


def encrypted_files(files):
    """从文件列表里挑出真正需要解码的（扩展名 + 文件头两种判据）"""
    out = []
    for p in files:
        ext = os.path.splitext(p)[1].lower()
        if ext in ENCRYPTED_EXT:
            out.append(p)
            continue
        if ext in (".flac", ".mp3"):
            # 扩展名可能是假的（酷狗/酷我把加密文件改名成 .flac/.mp3），
            # 所以再看一眼文件头
            try:
                with open(p, "rb") as fh:
                    head = fh.read(12)
            except OSError:
                continue
            if any(head.startswith(h) for h in _ENC_HEADERS):
                out.append(p)
    return out

_ffprobe_cache = {}


def _load_tag_module(tag_py):
    """动态加载 tag_files.py，拿它的打标签实现"""
    spec = importlib.util.spec_from_file_location("tagmod", tag_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _decode_console_bytes(raw):
    """把子进程 stdout 的原始字节解码成路径文本。返回 (文本, 是否损坏)

    为什么不能直接 `encoding="utf-8"`
    ---------------------------------
    PowerShell 5.1 往**管道**写字符串时用的是控制台输出编码（中文系统上
    是 OEM 代码页 936/GBK），**不是** UTF-8。之前这里写死 `encoding="utf-8"`
    配 `errors="replace"`，结果是：

        E:\\音乐\\KugouMusic\\李润祺 - 茫.kgg        （磁盘上真实存在）
        E:\\????\\KugouMusic\\?????? - ?.kgg        （程序实际拿到的）

    每个中文字符变成 U+FFFD，路径当然不存在，文件被判成「没选中」——
    而且因为 errors="replace" **不抛异常**，整条链路静默失败，日志只说
    「展开为 0 个音频文件」，查不出原因。2026-10-15 用户导
    `李润祺 - 茫.kgg` 失败就是这个 bug：纯 ASCII 文件名能过（两种编码
    解码结果一样），一带中文就废。

    现在按顺序试：UTF-8 → GBK。两个都行不通说明解码方式对不上，
    此时才报「损坏」，交给调用方提示用户（而不是悄悄给个死路径）。
    """
    if not raw:
        return "", False
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), False
        except UnicodeDecodeError:
            continue
    # 两种编码都解不出：退回替换式解码，并明确标记损坏
    return raw.decode("gbk", errors="replace"), True


def pick(mode, picker=None):
    """弹出系统对话框，返回选中的路径列表（取消则返回空列表）

    编码不可靠时**宁可一条都不返回**（外层会提示用户重选），也不要返回
    一串乱码路径 —— 那种路径后面只会以「展开为 0 个音频文件」的形式
    静默失败，比直接报错更难查。
    """
    script = picker or PICKER
    if not os.path.exists(script):
        return []
    try:
        # 不传 text/encoding：先拿原始字节，自己决定怎么解（见上）
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", script, "-Mode", mode],
            capture_output=True, timeout=600)
    except Exception:
        return []

    raw = r.stdout or b""
    if isinstance(raw, str):           # 万一将来被改成 text=True
        raw = raw.encode("utf-8", errors="replace")
    text, corrupt = _decode_console_bytes(raw)
    if corrupt:
        # 不返回乱码；把文件选择框的原话附上，方便定位
        err = (r.stderr or b"").decode("gbk", errors="replace").strip()
        print("⚠ 文件选择框的返回值无法解码（路径可能含非 ASCII 字符）"
              + (f"：{err[:200]}" if err else ""), file=sys.stderr)
        return []
    return [l.strip() for l in text.splitlines() if l.strip()]


def probe(path, ffprobe):
    """用 ffprobe 读真实规格。返回 dict 或 None"""
    if path in _ffprobe_cache:
        return _ffprobe_cache[path]
    info = None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-select_streams", "a:0",
             "-show_entries",
             "stream=codec_name,sample_rate,channels,bits_per_raw_sample,bit_depth,"
             "bits_per_sample",
             "-show_entries", "format=duration",
             "-of", "json", path],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
        j = json.loads(r.stdout or "{}")
        st = (j.get("streams") or [{}])[0]
        bits = (st.get("bits_per_raw_sample") or st.get("bit_depth")
                or st.get("bits_per_sample") or 0)
        rate = int(st["sample_rate"]) if st.get("sample_rate") else 0
        info = {
            "codec": st.get("codec_name") or "",
            "rate": rate,
            "bits": int(bits) if bits else 0,
            "ch": st.get("channels") or 0,
            "dur": float((j.get("format") or {}).get("duration") or 0),
        }
    except Exception:
        info = None
    _ffprobe_cache[path] = info
    return info


def is_lossless(path, info):
    """判断是否无损。优先看实测编码，其次看扩展名。"""
    if info and info.get("codec"):
        return info["codec"].lower() in ("flac", "alac", "ape", "wav",
                                         "pcm_s16le", "pcm_s24le",
                                         "pcm_s32le", "wmalossless")
    return os.path.splitext(path)[1].lower() in LOSSLESS_EXT


def quality_score(info):
    """规格评分，用于同名去重择优（越大越好）"""
    if not info:
        return -1
    lossless = 1 if info.get("codec", "").lower() in (
        "flac", "alac", "ape", "wav", "pcm_s16le", "pcm_s24le",
        "pcm_s32le") else 0
    return (lossless * 10 ** 12) + (info.get("bits") or 0) * 10 ** 9 \
        + (info.get("rate") or 0) * 1000


def collect(paths, with_encrypted=False, diag=None):
    """把「文件或文件夹」的混合列表展开成音频文件列表

    with_encrypted=True 时把加密容器也算进来 —— 调用方会先把它们解码
    再进入正常导入流程。

    diag 是**可选的**「为什么没收下它」记录本（一个 dict，键为原因、
    值为文件列表）。传它进来，被跳过的每一项都会被记下原因。

    为什么要有 diag（踩过的坑）
    --------------------------
    原来只有一句 `共选中 N 项，展开为 M 个音频文件`。一旦 M=0，用户看到
    的只有「没有新增文件（全部跳过或失败）」——**完全不知道是路径不存在、
    扩展名不认识，还是解码器没配**。2026-10-15 那次导入失败就是这样：
    真实原因是路径编码坏了导致文件"不存在"，但日志里一个字都没提，
    只能靠猜。所以这里规定：**任何一个没被收下的项，都必须留下原因。**
    """
    want = AUDIO_EXT + (ENCRYPTED_EXT if with_encrypted else ())

    def _note(reason, p):
        if diag is not None:
            diag.setdefault(reason, []).append(p)

    out = []
    for p in paths:
        if not os.path.exists(p):
            # 最容易被忽略的一种：路径本身是坏的（编码错、盘符错、文件被移走）
            _note("路径不存在", p)
            continue
        if os.path.isfile(p):
            base = os.path.splitext(p)[1].lower()
            if base in want:
                out.append(p)
                continue
            # 扩展名不在名单里 —— 但内容可能是加密的（酷狗爱把加密文件
            # 改名成 .flac/.mp3）。内容判据与 encrypted_files() 保持一致，
            # 否则会出现「说它没被收下，又说它需要解码器」这种自相矛盾。
            if base in (".flac", ".mp3"):
                try:
                    with open(p, "rb") as fh:
                        head = fh.read(12)
                except OSError:
                    _note("读不出文件头", p)
                    continue
                if any(head.startswith(h) for h in _ENC_HEADERS):
                    out.append(p)
                    continue
            # 是加密容器，但这次没带解码器进来（with_encrypted=False）。
            # 单独记一类原因：这跟「格式不认识」是两回事，用户看到
            # 「需要解码器」才知道该去配 music_decode_exe。
            if base in ENCRYPTED_EXT:
                _note("需要解码器", p)
            else:
                _note("扩展名不支持", p)
        elif os.path.isdir(p):
            n_before = len(out)
            for root, _dirs, files in os.walk(p):
                for f in sorted(files):
                    if os.path.splitext(f)[1].lower() in want:
                        out.append(os.path.join(root, f))
            if len(out) == n_before:
                _note("文件夹里没有音频文件", p)
        else:
            _note("既不是文件也不是文件夹", p)
    return out


def decode_encrypted(files, decoder, ffprobe, workdir, log=print):
    """把加密音乐解码成普通 flac/mp3，返回 (可导入文件列表, 待清理目录)

    为什么手动导入也要走这一步
    --------------------------
    你手动挑的文件很可能是客户端下下来的 .ncm/.qmc/.kgm。这类文件用
    ffprobe 读不出格式与时长，直接进导入流程会被判成「读不出规格」而失败。
    所以先做一次**无损还原**（不是转码：ncm 里包 flac 就出 flac，包 mp3
    就出 mp3，字节一致），再走后续的校验 / 去重 / 打标签。

    只解码确认过是加密容器的文件；普通 flac/mp3 原样返回，一个字节都不动。
    """
    enc = encrypted_files(files)
    plain = [f for f in files if f not in enc]
    if not enc:
        return plain, None
    if not decoder or not os.path.exists(decoder):
        log(f"⚠ 有 {len(enc)} 个加密音乐，但找不到解码器（{decoder}），跳过这些文件")
        return plain, None

    outdir = os.path.join(workdir, "decoded")
    os.makedirs(outdir, exist_ok=True)
    log(f"检测到 {len(enc)} 个加密音乐，先解码（无损还原，不是转码）")

    def _attempt(src, path, force_kgg=False):
        """跑一次解码，返回 (能当音频读的产物文件名, 输出文本, 退出码)

        判成功**只看产物能不能当音频读**，不看退出码。
        为什么：解码器在「ncm 里没嵌封面」时会打印 [Warn] 并让退出码变 1
        （main.cpp 里 success 还带着警告），但音频其实已经完整还原了。
        光看退出码会把这类正常文件判成失败，等于白白丢掉用户的歌。
        """
        before = set(os.listdir(outdir))
        if force_kgg:
            # 注意变量名别叫 probe —— 那会遮蔽外层模块级的 probe() 函数。
            # 之前这里叫 probe，于是下面第 376 行的 `probe(...)` 被 Python
            # 判成「用了尚未赋值的局部变量」，直接抛 UnboundLocalError，
            # **整个导入流程崩掉**。所有加密音乐（.kgg/.ncm/.qmc…）都会踩到，
            # 2026-10-14 23:23 那次导入失败的 traceback 就是它。
            probe_copy = os.path.join(outdir, "_probe.kgg")
            try:
                shutil.copy2(src, probe_copy)
                r = subprocess.run([decoder, "-o", outdir, probe_copy],
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace",
                                   timeout=1800)
            finally:
                try:
                    os.remove(probe_copy)
                except OSError:
                    pass
        else:
            r = subprocess.run([decoder, "-o", outdir, path],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=1800)
        for f in sorted(set(os.listdir(outdir)) - before):
            info = probe(os.path.join(outdir, f), ffprobe)
            if info and info.get("dur"):
                return _fix_probe_name(f), \
                    (r.stdout or "") + (r.stderr or ""), r.returncode
        return None, (r.stdout or "") + (r.stderr or ""), r.returncode

    def _fix_probe_name(f):
        """把「按 .kgg 重试」留下的 _probe.xxx 改回歌手 - 歌名.xxx

        为什么必须改（2026-10-15 实测踩到）
        ----------------------------------
        force_kgg 那条路是把源文件复制成 _probe.kgg 再喂给解码器，而解码器
        是**按输出名反推歌曲信息**的：输入叫 _probe.kgg，产出的音频就叫
        _probe.flac，然后这个文件名被原样拷进曲库。
        后果是整条链路全线认不出它：文件名里没有「歌手 - 歌名」，
        打标签取不到歌手、封面与歌词查不到、手机上显示成一首无名的歌。
        所以这里必须改回原名，让下游拿到的是正常文件名。

        改不回去也不失败：名字难看总比把用户的歌丢掉好（返回原名继续走）。
        """
        if not f.startswith("_probe"):
            return f
        ext = os.path.splitext(f)[1]
        want = os.path.splitext(os.path.basename(src))[0]
        if not want:
            return f
        target = os.path.join(outdir, want + ext)
        try:
            if os.path.exists(target):
                os.remove(target)
            os.replace(os.path.join(outdir, f), target)
            return want + ext
        except OSError:
            return f

    for i, src in enumerate(enc, 1):
        name = os.path.basename(src)

        # 顺序：先内置（唯一能真解 .kgma/.kgm/.kgtemp 的路），再外置
        # （管 .ncm/.qmc*/.kgg）。外置解码器在酷狗那一族上只会「静默跳过」
        # 或「报成功但产出乱码」，所以让它排后面。
        good, out_text, rc = None, "", 0
        py_got = _decode_via_python(src, outdir)
        if py_got and os.path.exists(py_got):
            info = probe(py_got, ffprobe)
            if info and info.get("dur"):
                good = os.path.basename(py_got)
                log(f"  [{i}/{len(enc)}] ✓ 已解码（内置酷狗解密）: {name} -> {good}")
                plain.append(py_got)
                continue

        good, out_text, rc = _attempt(src, src)

        # 第一遍没成，且这是酷狗那一族的，就换名字再试一遍。
        # 原因见 _decode_via_kgg()：解码器按扩展名分流，而酷狗换过一次
        # 密钥方案之后，KGM 那条路认不出新文件头了。
        if good is None and (os.path.splitext(src)[1].lower() in KGM_FAMILY_EXT
                             or _starts_kgm(src)):
            good, out_text2, rc2 = _attempt(src, src, force_kgg=True)
            if good is not None:
                out_text, rc = out_text2, rc2
                log("        按 .kgg 路径重试成功（酷狗换过密钥方案，"
                    "按原名会被判成不支持）")

        if good is None:
            log(f"  [{i}/{len(enc)}] ✗ 解码失败，跳过: {name}"
                + (f"（退出码 {rc}）" if rc else ""))
            for line in [x for x in out_text.splitlines() if x.strip()][-4:]:
                log(f"        {line}")
            continue
        if rc != 0:
            # 有产物但退出码非 0：多半只是「没封面」这类警告，记一笔就好
            warn = [x for x in out_text.splitlines() if "Warn" in x or "警告" in x]
            if warn:
                log(f"        （解码器提示：{warn[0].strip()}）")
        log(f"  [{i}/{len(enc)}] ✓ 已解码: {name} -> {good}")
        # 只把**确认是音频的那一个**加进去 —— 解码失败时留下的 .bin 乱码
        # 不能跟着混进导入列表。
        plain.append(os.path.join(outdir, good))

    return plain, outdir


def import_songs(paths, cfg, ffprobe, tag_py, log=print, decoder=None,
                 replace=False):
    """执行导入。返回统计 dict

    decoder 是 music_decode.exe 的路径（可选）。给了它，选中的加密音乐
    （ncm/qmc/kgm/vpr/kgg）会先**解码成普通 flac/mp3** 再走后面的校验；
    不给就只收普通音频。

    replace=True 进入**替换模式**：曲库里同名的旧文件会被这份替换并删除，
    不看规格高低（用于「自动下载下错了歌」时手工纠正）。
    """
    lib = cfg["sync"]["downloads_dir"]
    accept_lossy = bool(cfg.get("import", {}).get("accept_lossy", True))
    block_rates = set(cfg.get("guard", {}).get("block_sample_rates") or [])
    block_ch = set(cfg.get("guard", {}).get("block_channels") or [])

    os.makedirs(lib, exist_ok=True)
    diag = {}
    raw = collect(paths, with_encrypted=bool(decoder), diag=diag)
    log(f"共选中 {len(paths)} 项，展开为 {len(raw)} 个音频文件")

    # 一个都没展开出来时，**逐项说清为什么**。以前这里只有一句
    # 「没有新增文件」，用户拿着日志也查不出问题（见 collect() 的注释）。
    if not raw and diag:
        log("⚠ 选中的内容没有被识别为音频，逐项原因：")
        for reason, items in diag.items():
            for p in items[:10]:
                log(f"    · [{reason}] {p}")
            if len(items) > 10:
                log(f"    · …另有 {len(items) - 10} 项同样是「{reason}」")
    # 「识别到了加密音乐，但没配解码器」也要说出来 —— 否则用户只会看到
    # 「展开为 0 个音频文件」，完全想不到是解码器路径没配。
    if not raw and not decoder:
        enc_all = encrypted_files(paths)
        if enc_all:
            log(f"⚠ 其中有 {len(enc_all)} 个是加密音乐容器，"
                "但没配置解码器（设置里的 music_decode_exe），只能跳过")
            for p in enc_all[:5]:
                log(f"    · {p}")

    # ---- 0. 加密音乐先解码（无损还原，改的是容器不是音质）----
    decoded_dir = None
    if decoder:
        workdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "_import_tmp")
        files, decoded_dir = decode_encrypted(raw, decoder, ffprobe, workdir, log)
    else:
        files = raw

    try:
        return _import_plain(files, lib, cfg, ffprobe, tag_py,
                             accept_lossy, block_rates, block_ch, log,
                             replace=replace)
    finally:
        # 解码出来的临时副本用完就删 —— 真正入库的是拷进同步文件夹那份
        if decoded_dir:
            shutil.rmtree(os.path.dirname(decoded_dir), ignore_errors=True)


def _norm_stem(stem):
    """同名判定用的规范化：全角→半角、大小写、空白。

    只抹平「写法差异」，**不动标点** —— 否则 "Song (Live)" 会和 "Song"
    被判成同一首，替换模式就会删错文件。
    """
    s = unicodedata.normalize("NFKC", stem or "").casefold()
    return re.sub(r"\s+", "", s)


def _spec(info):
    """把 probe() 的结果写成「96kHz/24bit flac」这种人看的样子"""
    if not info:
        return "读不出规格"
    rate = (info.get("rate") or 0) / 1000
    return (f"{rate:g}kHz"
            + (f"/{info['bits']}bit" if info.get("bits") else "")
            + f" {info.get('codec') or '?'}")


def _drop_same_name(lib, stem, keep_path, log=print):
    """删掉曲库里所有「同名（规范化后相同）」的旧音频，keep_path 留着。

    替换模式专用：名字相同就是同一首，旧的那份必须走人，否则曲库里会
    出现两首同名的歌 —— 用户报的「不同的歌叫同一个名字」就是这个现象。
    """
    want = _norm_stem(stem)
    keep = os.path.abspath(keep_path)
    gone = 0
    try:
        names = os.listdir(lib)
    except OSError:
        return gone
    for fn in names:
        base, e = os.path.splitext(fn)
        if e.lower() not in AUDIO_EXT:
            continue
        if _norm_stem(base) != want:
            continue
        p = os.path.join(lib, fn)
        if os.path.abspath(p) == keep:
            continue
        try:
            os.remove(p)
            gone += 1
            log(f"      已删除同名旧文件: {fn}")
        except OSError as exc:
            log(f"      ⚠ 同名旧文件删不掉（可能正被同步/播放占用）: {fn}（{exc}）")
    return gone


def _replace_retry(tmp, dst, attempts=6, delay=0.8):
    """os.replace 的容错版（替换模式要覆盖的正是已有文件）。

    曲库是 Syncthing 的 sendonly 目录，它偶尔正在读某个文件时会给它加锁
    （WinError 32），此时 os.replace 直接失败。实测这类锁只持续几秒，
    重试即可，所以这里退避重试而不是一撞就报错。
    """
    last = None
    for _ in range(attempts):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError as exc:      # WinError 32
            last = exc
            time.sleep(delay)
    raise last if last is not None else OSError("replace 失败")


def _import_plain(files, lib, cfg, ffprobe, tag_py,
                  accept_lossy, block_rates, block_ch, log=print,
                  replace=False):
    """导入流程本体（文件都已经是普通音频了）

    replace=True 时进入**替换模式**：同名即替换，不看规格高低，
    并把曲库里同名的其它旧文件一并删掉。
    """
    if not files:
        return {"total": 0, "ok": 0, "skip": 0, "fail": 0,
                "tagged": 0, "replaced": 0, "imported": []}

    tagmod = None
    try:
        tagmod = _load_tag_module(tag_py)
    except Exception as exc:
        log(f"⚠ 打标签模块加载失败（将跳过打标签）: {exc}", )

    stat = {"total": len(files), "ok": 0, "skip": 0, "fail": 0, "tagged": 0,
            "replaced": 0, "imported": []}
    for i, src in enumerate(files, 1):
        name = os.path.basename(src)
        stem = os.path.splitext(name)[0]
        ext = os.path.splitext(name)[1].lower()

        # ---- 1. 规格校验 ----
        info = probe(src, ffprobe)
        if info is None:
            log(f"  [{i}/{len(files)}] ✗ 读不出规格（可能不是音频）: {name}")
            stat["fail"] += 1
            continue
        if not info.get("dur"):
            log(f"  [{i}/{len(files)}] ✗ 时长为 0，文件可能损坏: {name}")
            stat["fail"] += 1
            continue

        lossless = is_lossless(src, info)
        if not accept_lossy and not lossless:
            log(f"  [{i}/{len(files)}] ⊘ 跳过（有损，且设置未允许）: {name}")
            stat["skip"] += 1
            continue

        # ---- 2. 安全检查：拦下会伤害设备的属性 ----
        if block_rates and info.get("rate") in block_rates:
            log(f"  [{i}/{len(files)}] ✗ 拦截（{info['rate']} Hz 在拦截名单里）: {name}")
            stat["skip"] += 1
            continue
        if block_ch and info.get("ch") in block_ch:
            log(f"  [{i}/{len(files)}] ✗ 拦截（{info['ch']} 声道在拦截名单里）: {name}")
            stat["skip"] += 1
            continue

        # ---- 3. 同名去重 ----
        dst = os.path.join(lib, name)
        overwriting = False
        if replace:
            # 替换模式：名字相同 = 同一首歌，**一律以这次导入的为准**。
            #
            # 为什么这里故意不看规格（与下面的「自动择优」相反）：
            # 用户开这个模式，正是因为自动下载「下错了歌」。错的那份常常
            # 规格更高（比如下成 96kHz 的另一首），若还按「规格高者胜」
            # 处理，正确的这份会被直接跳过 —— 那这个功能就等于没做。
            #
            # 注意：这里只判断、不删。删旧文件放到「新文件已经安全落盘」
            # 之后（见步骤 4 末尾）—— 万一拷贝失败，旧的还在，不会两头空。
            overwriting = os.path.exists(dst)
            if overwriting:
                old = probe(dst, ffprobe)
                log(f"  [{i}/{len(files)}] ↻ 替换同名旧文件"
                    f"（旧 {_spec(old)} → 新 {_spec(info)}）")
        else:
            if os.path.exists(dst):
                same = os.path.getsize(src) == os.path.getsize(dst)
                if same:
                    log(f"  [{i}/{len(files)}] ⊘ 已存在且大小相同: {name}")
                    stat["skip"] += 1
                    continue
                old = probe(dst, ffprobe)
                if quality_score(old) >= quality_score(info):
                    log(f"  [{i}/{len(files)}] ⊘ 已存在且规格不低于导入版: {name}")
                    stat["skip"] += 1
                    continue
                log(f"  [{i}/{len(files)}] ↑ 已存在但规格更低，替换: {name}")

            # 同名不同扩展名：先清掉其它扩展名的旧版本
            for other_ext in AUDIO_EXT:
                other = os.path.join(lib, stem + other_ext)
                if other_ext != ext and os.path.exists(other):
                    try:
                        os.remove(other)
                        log(f"      移除同名异扩展名旧文件: {os.path.basename(other)}")
                    except OSError:
                        pass

        # ---- 4. 拷进同步文件夹 ----
        try:
            tmp = dst + ".importing-" + uuid.uuid4().hex[:6]
            shutil.copy2(src, tmp)
            # 原子替换，避免同步到半个文件。替换模式下 dst 本来就存在，
            # 而 Syncthing 可能正在读它（会短暂加锁），所以走带重试的版本。
            _replace_retry(tmp, dst)
        except Exception as exc:          # noqa: BLE001
            log(f"  [{i}/{len(files)}] ✗ 拷贝失败: {exc}")
            stat["fail"] += 1
            continue

        spec = (f"{info['rate'] / 1000:g}kHz"
                + (f"/{info['bits']}bit" if info.get("bits") else "")
                + f" {info['codec']}")
        log(f"  [{i}/{len(files)}] ✓ 已导入 ({spec}) {name}")
        stat["ok"] += 1
        stat["imported"].append(stem)

        # 替换模式：**等新文件真的落盘了**才清掉同名的旧副本，
        # 这样即使拷贝失败也不会出现「旧的删了、新的没进来」。
        if replace:
            if overwriting:
                stat["replaced"] += 1
            stat["replaced"] += _drop_same_name(lib, stem, dst, log)

        # ---- 5. 打标签（复用 tag_files，按文件头判断格式）----
        if tagmod is not None:
            try:
                artist, title = tagmod.split_stem(stem)
                tagmod.write_tags(dst, artist, title)
                stat["tagged"] += 1
            except Exception as exc:      # noqa: BLE001
                log(f"      标签写入失败: {exc}")

    log("")
    log(f"导入完成：成功 {stat['ok']}，跳过 {stat['skip']}，失败 {stat['fail']}"
        + (f"，已写标签 {stat['tagged']}" if stat["tagged"] else "")
        + (f"，替换同名旧文件 {stat['replaced']} 个" if stat["replaced"] else ""))
    return stat


def trigger_scan(cfg, log=print):
    """让 Syncthing 立刻扫描，不必等它的定时任务（默认 300 秒）"""
    s = cfg.get("sync") or {}
    api_key = s.get("api_key")
    folder = s.get("folder_id")
    gui = (s.get("syncthing_gui") or "http://127.0.0.1:8384").rstrip("/")
    if not api_key or not folder:
        log("（Syncthing 未配置，跳过触发扫描；文件会在下次同步时自动上传）")
        return False
    try:
        req = urllib.request.Request(
            f"{gui}/rest/db/scan?folder={folder}",
            headers={"X-API-Key": api_key}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read(1)
        log("已触发 Syncthing 扫描，稍后会自动传到手机")
        return True
    except Exception as exc:              # noqa: BLE001
        log(f"触发扫描失败（不影响文件已就位）: {exc}")
        return False
