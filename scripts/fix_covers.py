#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给曲库里的歌曲补封面（可选补歌词）。

背景
----
go-music-dl 引擎有两条下载路径：

  GET  /music/download?embed=1              -> 走 DownloadSongData 分支，**会**嵌封面
  POST /music/download?embed=1&save_local=1 -> 走 DownloadWithDedup 分支，落盘但**不**嵌封面

music-monitor 为了拿到 JSON 结果（判断 skipped / filename），必须用 POST +
save_local=1，于是掉进了不嵌封面那条路。实测同一首歌：

  GET  : 24,365,502 字节，FLAC 里 IMAGE_BLOCK=1   <- 有封面
  POST : 24,347,592 字节，FLAC 里 IMAGE_BLOCK=0   <- 没封面

差 17,910 字节，正好一张封面。引擎也不会报错（响应头连 warning 都没有）。

实测曲库 268 首里只有 7 首有封面，且全是**用户自己导入的原盘文件**
（encoder 标签是 XL Recordings / X Lossless Decoder），酷狗下的**一首都没有**。

做法
----
既然引擎不嵌，就在本地补齐。歌曲名/歌手从文件名 "歌手 - 歌名.ext" 解析，
封面按以下顺序取（都是免登录公开接口）：

  1. 网易云 搜索 -> 专辑 -> picUrl        （一般 100~200 KB，清晰度高）
  2. iTunes Search API artworkUrl100 换 600x600  （保底，华语曲目命中率一般）

匹配必须**歌手名对得上**才采用，避免张冠李戴。歌词可选（--lyrics），
同样按这个流程取，写进 FLAC 的 LYRICS 标签。

支持 flac / mp3 / m4a / ogg。按**文件头**判断容器，不看扩展名
（酷狗有大量伪装成 .mp3 的 FLAC）。写入前先写临时文件再原子替换，
避免断电/中断把文件写坏。

用法
----
  python scripts/fix_covers.py                      # 只报告缺多少
  python scripts/fix_covers.py --limit 3 --apply    # 先小批量试
  python scripts/fix_covers.py --apply              # 全量补
  python scripts/fix_covers.py --apply --lyrics     # 顺带补歌词
  python scripts/fix_covers.py --apply --force      # 连已有的也覆盖
  python scripts/fix_covers.py --apply --only "Aimer - Brave Shine"
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import ID3, TIT2, TPE1, APIC, USLT, COMM, ID3NoHeaderError
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggvorbis import OggVorbis
except ImportError:
    sys.exit("需要 mutagen：python -m pip install mutagen")

DEFAULT_DIR = r"D:\go-music-dl\downloads"
AUDIO_EXT = (".flac", ".mp3", ".m4a", ".ogg")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TIMEOUT = 25
MAX_COVER_BYTES = 800 * 1024   # 单张封面体积上限，超了就换小一档

# 三种结果，必须区分开：
#   OK    取到了
#   MISS  接口正常回应，但确实没有这一首（重跑也没用，跳过是合理的）
#   BUSY  被限流/网络故障（**不是**没有，稍后重跑还能取到）
#
# 踩过的坑：一开始这两种情况都返回 None，于是一次限流之后，
# 脚本把剩下几百首都当成「没找到」静默跳过，看起来像正常跑完。
OK, MISS, BUSY = "ok", "miss", "busy"

# 网易云对同 IP 的搜索接口限得很死，撞上了先歇一会儿再试，
# 而不是继续打（继续打只会让冷却时间越来越长）。
COOLDOWN_BASE = 15.0
COOLDOWN_MAX = 180.0
_cooldown_until = 0.0
# 连续被限流多少次就收工。限流期间继续跑只是白白等冷却、把整轮拖成几小时，
# 不如明确退出让用户隔一会儿重跑（脚本本身是幂等的，已经有的不会再取）。
ABORT_AFTER_BUSY = 8


class RateLimited(Exception):
    """网易云返回 code=405「操作频繁」。"""


def _cooldown_left() -> float:
    return max(0.0, _cooldown_until - time.time())


def _enter_cooldown(seconds: float) -> None:
    global _cooldown_until
    _cooldown_until = max(_cooldown_until, time.time() + seconds)


# --------------------------------------------------------------------- 网络

def http_get(url: str, referer: str = "https://music.163.com/") -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def http_json(url: str, referer: str = "https://music.163.com/") -> dict:
    d = json.loads(http_get(url, referer).decode("utf-8", "replace"))
    # 网易云的限流不是 HTTP 429，而是 HTTP 200 + body 里 code=405。
    # 不检查这个就会把限流当成「没搜到」。
    if isinstance(d, dict) and d.get("code") == 405:
        raise RateLimited(d.get("message") or d.get("msg") or "操作频繁")
    return d


# 封面至少要有这么多像素的边长。低于它的图基本都是平台缩略图，或者
# 干脆就是「没有封面」时返回的默认图 —— 实测网易云对缺封面的专辑会吐一张
# 320×320 灰底占位图（带「网易云音乐」水印），才 8.6 KB。这种图嵌进歌里
# 比没封面更糟，所以判为「太小」，让调用方换源或干脆不配。
MIN_COVER_PX = 500
# 封面几乎总是 1:1。长宽差超过短边的这个比例就认为不是正经封面（实测抓到
# `方大同 - 麦恩莉` 的 1535x1425，差 7.3%）。
MAX_COVER_ASPECT_DEV = 0.05


def image_dims(data: bytes) -> tuple[int, int] | None:
    """只读文件头拿 (宽, 高)，不依赖任何图形库。

    支持 JPEG（扫 SOF 段）和 PNG（IHDR 固定偏移）。认不出来返回 None ——
    调用方应当把 None 当作「不知道」，而不是「太小」。
    """
    if data[:3] == b"\xff\xd8\xff":
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:       # 段边界对不上，别再瞎扫
                return None
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2                # 无长度字段的标记
                continue
            seglen = int.from_bytes(data[i + 2:i + 4], "big")
            # SOF0..SOF15，跳过 DHT(C4)/JPG(C8)/DAC(CC) 这三个非 SOF
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return w, h
            i += 2 + seglen
        return None

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return w, h

    return None


def _pixel_area(data: bytes) -> int:
    """像素面积（宽 x 高），用来在合格封面之间比清晰度。认不出尺寸算 0。"""
    d = image_dims(data)
    return d[0] * d[1] if d else 0


def _is_square(data: bytes) -> bool:
    """严格正方形（宽高完全相等）。认不出尺寸算 False。

    比面积时先过这一关：面积对长方形天然有利，`1080x1082` 只差 0.2%，
    能过 cover_too_small 的宽容线，但拿它当封面就是错的比例。
    """
    d = image_dims(data)
    return d is not None and d[0] == d[1]


def cover_too_small(data: bytes) -> bool:
    """是不是缩略图/占位图。认不出尺寸时宁可放行（返回 False）。

    两条判据：
      - 最长边 < MIN_COVER_PX（缩略图）
      - **不是正方形**（实测 `方大同 - 麦恩莉` 拿到 1535x1425 的截图式图片，
        音乐库封面几乎总是 1:1，长宽比明显不对就换源）
    """
    dims = image_dims(data)
    if dims is None:
        return False
    w, h = dims
    if min(w, h) < MIN_COVER_PX:
        return True
    return abs(w - h) > max(w, h) * MAX_COVER_ASPECT_DEV


# ---------------------------------------------------------------- 纯色图判定
# 网易云对「没有封面」的专辑会返回一张**纯色图**（实测 600x600、约 2.5–7KB、
# 像素标准差 0.0）。尺寸正常、体积正常，靠尺寸完全拦不住，必须看像素。
#
# 目标环境没装 PIL，所以用宿主自带的 .NET System.Drawing 解码：把图喂给
# PowerShell 侧的辅助脚本，返回 (均值, 标准差, 最暗, 最亮)。
PIXEL_HELPER = r"D:\music-flow\scripts\pixel_stats.ps1"


def pixel_stats(data: bytes) -> tuple[float, float] | None:
    """返回 (平均亮度, 亮度标准差)。拿不到就返回 None（当作「不知道」）。"""
    if not os.path.exists(PIXEL_HELPER):
        return None
    fd, tmp = tempfile.mkstemp(suffix=".img")
    os.close(fd)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", PIXEL_HELPER, "-Path", tmp],
            capture_output=True, text=True, timeout=60,
        )
        txt = (out.stdout or "").strip()
        if not txt:
            return None
        mean, std = txt.split(",")[:2]
        return float(mean), float(std)
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def cover_is_flat(data: bytes, max_std: float = 3.0) -> bool:
    """纯色/近乎纯色的图 —— 不是封面，是占位图。

    标准差阈值取 3.0：真实封面即便大面积留白也会有文字、边缘、噪点，
    标准差远大于 3；占位图实测 0.0。
    """
    st = pixel_stats(data)
    if st is None:
        return False          # 测不了就当没问题，宁可放行
    _, std = st
    return std < max_std


def norm(s: str) -> str:
    """归一化歌手/歌名，用于宽松比对：去掉空格、标点、全角符号，转小写。"""
    out = []
    for ch in (s or "").lower():
        if ch.isalnum():
            out.append(ch)
    return "".join(out)


def artist_matches(want: str, got: str) -> bool:
    """歌手名是否对得上。支持 'A、B' 这种多歌手（任一命中即可）。"""
    w = norm(want)
    if not w:
        return False
    for part in (got or "").replace("、", "/").replace(",", "/").replace("&", "/").split("/"):
        g = norm(part)
        if g and (g in w or w in g):
            return True
    return False


# 括号里的东西基本是版本说明（(Live)/(DJ版)/(原唱 周杰伦)/(Remastered)…），
# 比对歌名时要先剥掉，否则 "晴天" 和 "晴天 (Live)" 会被算成两个不同的歌名。
_VERSION_PAREN = re.compile(r"[（(\[【][^）)\]】]*[）)\]】]")

# 歌名里**自带**翻唱声明的写法（网易云的翻唱投稿很爱这么写）：
#     `Amazing Trees（Cover Tielle / SawanoHiroyuki[nZk]）`
#     `晴天 (Cover 周杰伦)`
# 这种"封面"字在里面是正儿八经的翻唱标记，不是专辑名里那个字母 u 的意外
# 命中 —— 所以要连 Cover 后面的**署名**一起匹配才作数。`\b` 是必须的：
# 没有它 "Dis**cover**y" 会被当成 `cover` + 署名 "y"（实测踩过）。
_COVER_CREDIT_MARK = re.compile(
    r"\b(?:cover|翻自|翻唱)\b\s*(?:by\s*)?[:：/／]?\s*[\w\u4e00-\u9fff]",
    re.I,
)


def title_core(title: str) -> str:
    """剥掉版本后缀再归一化，用于歌名比对。"""
    return norm(_VERSION_PAREN.sub("", title or ""))


def title_similar(want: str, got: str) -> float:
    """歌名相似度 0~1。刻意做成多档，而不是「剥完括号相等就算 1.0」。

    因为库里存的是**原始曲目名**，而括号里的东西有时是版本、有时是歌名本身
    的一部分。全剥掉的话 "Hello" 和 "Hello (Dash Berlin Rework)"、
    "Hello (Live)" 会并列满分 —— 实测就是这样把 Adele《Hello》配成了
    混音版的封面（图不对）。所以：
      - 完全一字不差            -> 1.00  （最优，永远排最前）
      - 剥掉括号后完全相同      -> 0.85  （是同一首歌的某个版本）
      - 一方包含另一方          -> 0.80
      - 其余按字面相似度打折    -> 0~0.75
    """
    w_raw, g_raw = norm(want), norm(got)
    if not w_raw or not g_raw:
        return 0.0
    if w_raw == g_raw:
        return 1.0
    w, g = title_core(want), title_core(got)
    if not w or not g:
        return 0.0
    if w == g:
        return 0.85
    if w in g or g in w:
        return 0.80
    return 0.75 * difflib.SequenceMatcher(None, w, g).ratio()


def artist_similar(want: str, got: str) -> float:
    """歌手相似度 0~1，比 artist_matches 宽（后者只给 0/1）。

    为什么要宽：网易云的歌手字段经常不是本名 —— 搜「晴天 周杰伦」，返回的
    候选里歌手写作 "Jay"、"RyaVocal"（翻唱）、"GYBeat"，一条真名都没有。
    只认「一模一样」，这首歌就会一张封面都配不上；给个相似度再综合排序，
    配上歌名的高分，就能把 "Jay - 晴天" 这种正确条目挑出来。
    """
    w = norm(want)
    if not w:
        return 0.0
    best = 0.0
    for part in (got or "").replace("、", "/").replace(",", "/").replace("&", "/").split("/"):
        g = norm(part)
        if not g:
            continue
        if g == w:
            return 1.0
        if g in w or w in g:
            best = max(best, 0.9)
        else:
            best = max(best, difflib.SequenceMatcher(None, w, g).ratio())
    return best


# 综合分的及格线。低于它就宁可不配封面 —— 配错封面比没封面更糟，
# 用户看到的是一张完全不相干的图。
MATCH_THRESHOLD = 1.05

# 候选的歌手**比我们要查的人多**时的降权（每多一个扣一次）。
#
# 起因：`Taylor Swift - Welcome To New York` 的第一名是
#     「The Ukulele Boys、Taylor Swift、Ryan / Welcome to New York」
# —— 尤克里里翻奏合辑，因为 Taylor 在里面挂了个署名，`artist_similar`
# 拆开比就给了满分 1.0，再白拿「专辑名 == 歌名」的加分，4.50 反超正主的
# 3.00。这类「我们的歌手只是候选的 N 个之一」的条目几乎都不是原曲。
#
# 取 1.0：小于 ALBUM_SAME_BONUS(1.5)，所以不会把 `Lost Sky - Vision` 里
# 那条「专辑名 == 歌名」的单曲顶掉（它歌手只有一个、不扣分，仍是唯一带
# 加分的候选）；又足以把「多挂了 2 个名字」的翻奏合辑压到正主下面。
COLLAB_PENALTY = 1.0


def _artist_part_count(s: str) -> int:
    """歌手字段里有几个歌手（网易云用 `、` 分隔）。"""
    return len([p for p in re.split(r"[、/,;&:]", s or "") if p.strip()])

# 歌名的**准入线**：歌名不到这条线，歌手再像也不采纳。
#
# 起因是实测的一次配错：搜 "Martin Garrix、DubVision、Jordan Grace - Oxygen"，
# 网易云返回的第一条是同专辑（Sentio）的《Oxygen》，但打分却选了另一首
# 《Scared To Be Lonely》—— 因为它们**歌手完全相同**，artist_similar 给了满分
# 1.0，把歌名的烂分数（SequenceMatcher 只有 0.1）硬扛过了 1.05 的及格线。
# 结果就是给《Oxygen》配上了《Scared To Be Lonely》的图。
#
# 所以歌手分只能用来**排序**，不能用来**翻案**。歌名先过 0.80
# （= 一字不差 / 剥掉括号后相同 / 一方包含另一方，见 title_similar），
# 过了线的候选之间才比歌手。
TITLE_MIN = 0.80

# 歌手的**准入线**（硬门槛，与 TITLE_MIN 并列，两个都得过）。
#
# 起因：整库实测发现一批歌被配上了**别人的翻唱版**，而歌名是一字不差的：
#     `林俊杰 - 当你`      <- 网易云给「任树 / 当你」
#     `薛之谦 - 野心`      <- 网易云给「杨贤文 / 野心」
#     `梁静茹 - 情歌`      <- 网易云给「鲁怀德 / 情歌」
#     `AGA - Wonderful U`  <- 网易云给「凌晨四点半 / Wonderful U」
# 根因和 TITLE_MIN 那次一模一样，只是方向反了：**歌名满分给 2.0，天生跨过
# MATCH_THRESHOLD=1.05**，所以「歌名一模一样但不是这个歌手」照样录取。
#
# 取值依据是整库 268 首的歌手相似度实测，分布是干净的双峰：
#     =1.0 完全相同 127 首 | 0.9-1.0 包含 103 首 | 0.6-0.9 **0 首** | <0.6 32 首
#   0.35 正好落在那段空档里。调到 0.50 会误伤 `JW - I Just Wanna Stay With
#   You`（网易云写的本名 `王灏儿`）和 `전소미 - XOXO`（网易云写罗马音
#   `JEON SOMI`）—— 那是同一个人的另一种写法，不是别人。
#
# **不能为了救别名而放宽这条线**（踩过的大坑）：网易云的歌手字段是
# **用户自由填写**的，同一个人换一种写法就完全不字面相似：
#     周杰伦 -> `Jay`          artist_similar = 0.000
#     JW     -> `王灏儿`        = 0.000
#     전소미  -> `JEON SOMI`    = 0.000
# 于是试过"歌名一字不差 **或** 歌手对得上"的二选一准入 —— 结果**13 首翻唱
# 版立刻回归**（翻唱的歌名本来就一字不差，等于把这道门槛废掉了）。
# 正确的做法是：歌手**硬卡**，别名走下面显式的 _ARTIST_ALIASES 表。
ARTIST_MIN = 0.35

# ---------------------------------------------------------------------------
# 艺人别名表：同一个人的**另一种写法**，字面上完全不像，但确实是本人。
#
# 为什么需要它：网易云的歌手字段是用户自由填写的，`周杰伦` 会写成 `Jay`，
# 相似度 0.000。如果只按字面卡歌手，**原唱本人会被自己的别名挡掉**，
# 整个歌变成"没有封面"。
#
# 为什么不能放宽歌手阈值来代替它：实测整库 268 首的歌手相似度是干净的双峰
# （=1.0 有 127 首、0.9-1.0 有 103 首、**0.6-0.9 有 0 首**、<0.6 有 32 首），
# 而那 32 首 <0.6 **全部是配错的翻唱版**（`林俊杰 - 当你` <- 「任树」、`薛之谦
# - 野心` <- 「杨贤文」…）。放宽就等于把翻唱全放进来 —— 试过"歌名一字不差
# 或歌手对得上"的二选一准入，13 首翻唱立刻回归。所以歌手必须硬卡，
# 别名只能显式列表。
#
# 维护方式：这是**只增不改**的确认表，每条都要亲眼确认过是本人。新增别名
# 走这里，不要去动 ARTIST_MIN。
_ARTIST_ALIASES: dict[str, set[str]] = {
    "周杰伦":     {"jay", "jay chou", "周杰倫"},
    "jw":         {"王灏儿", "王灝兒"},
    "전소미":      {"jeon somi", "全昭弥"},
}


def _alias_key(name: str) -> str:
    """归一化后的歌手名，用来查别名表。"""
    return norm(name).strip()


def _artist_agree(want: str, got: str) -> bool:
    """**逐个**歌手对逐个歌手地比，任一对上就算对上。

    为什么要逐个比：库里和接口两边的**顺序和分隔符都不统一**。实测
    `SawanoHiroyuki[nZk]:Tielle - Amazing Trees`：
        本地标签   SawanoHiroyuki[nZk]:Tielle     （冒号分隔）
        网易云候选 Tielle、SawanoHiroyuki[nZk]     （顿号分隔，而且顺序反了）
    按整串比（`artist_similar` 那种"拿整体当分母"的算法）歌手分只有 0.308，
    过不了 ARTIST_MIN=0.35，于是这张封面白白丢掉。逐个比就满分。
    """
    for a in re.split(r"[、/,;&:]", want or ""):
        wa = norm(a)
        if not wa:
            continue
        for b in re.split(r"[、/,;&:]", got or ""):
            wb = norm(b)
            if wb and (wa == wb or wa in wb or wb in wa):
                return True
    return False


def artist_ok(want: str, got: str) -> bool:
    """歌手算不算对得上 —— 字面相似过线（或逐个歌手对上），**或者**命中已确认的别名表。"""
    if _artist_agree(want, got) or artist_similar(want, got) >= ARTIST_MIN:
        return True
    w = _alias_key(want)
    g = _alias_key(got)
    for canon, aliases in _ARTIST_ALIASES.items():
        group = {_alias_key(canon)} | {_alias_key(a) for a in aliases}
        if w in group and g in group:
            return True
    return False


# 「专辑名 == 歌名」的加分。用来把单曲/同名专辑顶到厂牌合辑前面（见
# netease_search 的说明）。取 1.5 而不是 1.0：实测 `Lost Sky - Vision` 的
# 合辑条目和单曲条目歌名/歌手分完全相同（都是 3.0），必须有一个明确的
# 差值才能稳定改变顺序；1.5 足够大，又不会让一个歌名不上线的条目挤进来
# （准入仍然是 TITLE_MIN 卡死的）。
ALBUM_SAME_BONUS = 1.5

# 歌名里带版本标记（翻版/剪辑版/混音版…）的降权。取 2.0：正好等于「歌名
# 一字不差」的满分权重，命中就足以把任何一个翻版压到正主之下，同时因为
# 准入仍看 TITLE_MIN，不会把正经的 Remix 变成"没有封面"。
VERSION_PENALTY = 2.0

# 版本标记词。实测撞到的：`晴天 (钢琴版) [原唱: 周杰伦]`、`Hello (Workout
# Mix)`、`孤勇者 (童声版)`、`Brave Shine (TV size)`、`Quit (Wild Kidz
# Remix)`。注意**不能**拿它去否决用户曲库里的文件名 —— 库里本来就有
# Remix / Live / Radio Edit / Extended Mix，那些是要正常配图的；这里
# 降的只是「搜出来的候选相对用户这首歌」的偏离。
#
# 每种写法都带 **ASCII 词边界**（`(?<![a-z])…(?![a-z])`），不能只写 `live`。
# 踩过的坑：`唯有追赶风的方向 (Only By Chasing the Wind)` —— 本地标题里
# 有 **Only**，而 `only` 里正好含着 `ly`… 于是 `live` 被判成"候选多出来的
# 版本标记"，整首被扣分丢掉。同理会误伤 `deliver` / `silver` / `alive` /
# `olive`。加边界后 `live` 只匹配独立的单词，不再匹配单词内部的片段。
# （`\b` 对 CJK 不适用 —— 中文与 ASCII 之间本来就没有词边界，所以用
# 否定环视把「前面/后面不是小写字母」写死。）
_VERSION_WORDS = [
    r"tv\s*(?:size|ver|version)", r"live", r"remix", r"mix", r"edit",
    r"version", r"ver\.", r"cover", r"inst", r"instrumental", r"karaoke",
    r"acoustic", r"demo", r"remaster(?:ed)?", r"slowed", r"sped\s*up",
    r"nightcore", r"8d",
]
# 这几类是单个汉字，且本身不构成更长英文单词，不需要边界
_VERSION_CJK = [
    "伴奏", "钢琴", "吉他", "童声", "童聲", "女声", "男声", "翻自", "翻唱",
    "原唱", "抖音", "铃声", "慢摇", "dj",
]
VERSION_TAG_RE = re.compile(
    "|".join([rf"(?<![a-z])(?:{w})(?![a-z])" for w in _VERSION_WORDS] + _VERSION_CJK),
    re.IGNORECASE)


def version_tagged(want: str, got: str) -> bool:
    """候选歌名里带的版本标记，是 want 里**没有**的吗。

    只要 want 里有同样的词（`Extended Mix` 这首歌本来就带 "mix"），就不算
    偏离，避免把用户库里正儿八经的 Remix 版本一棍子打死。
    """
    w = norm(want)
    g = norm(got)
    for m in VERSION_TAG_RE.finditer(got or ""):
        tag = norm(m.group(0))
        if tag and tag not in w and tag in g:
            return True
    return False


# 翻唱/伴奏/致敬盘的标记词。这类曲目的标题和原曲**几乎一样**，纯靠歌名
# 比对根本挡不住 —— 实测就放进来两条：
#   "Mama (Originally Performed by Jonas Blue)"  ← 不是 Jonas Blue 本人的版本
#   "Rescue Me [Originally Performed by ...]"
# 它们配的封面是翻唱合辑的图，跟原曲八竿子打不着。
#
# 只挡「显然不是原唱」的：Originally Performed / Karaoke / Cover / Tribute /
# Instrumental / 伴奏。**不挡** Remix / Live / Remaster / Acoustic / Demo ——
# 库里本来就存着这类版本（"Jin - Last Summer (K.TOBI Remix)"、
# "Karra - Where We Are (Radio Edit)"），挡掉反而会误伤。
_TRIBUTE_MARK = re.compile(
    r"originally\s*performed|made\s+\w+\s+by|in\s*the\s*style\s*of|tribute|"
    r"karaoke|instrumental|backing\s*track|伴奏|翻唱|纯音乐",
    re.I,
)


def looks_like_tribute(title: str) -> bool:
    """标题自带翻唱/伴奏标记 —— 这种候选不该拿来当原曲的封面。"""
    if _COVER_CREDIT_MARK.search(title or ""):
        return True
    # 库里存着 "Forest Mixtape (纯钢琴)" 这类本地化的版本说明，所以这里也认
    # 「纯钢琴 / 钢琴版」，它们和「纯音乐」是同一类（演奏版，不是原曲）。
    if re.search(r"纯钢琴|钢琴版", title or ""):
        return True
    return bool(_TRIBUTE_MARK.search(title or ""))


def split_stem(stem: str) -> tuple[str, str]:
    """'歌手 - 歌名' -> (歌手, 歌名)"""
    parts = stem.split(" - ")
    if len(parts) >= 2:
        return parts[0].strip(), " - ".join(parts[1:]).strip()
    return "", stem.strip()


# ----------------------------------------------------------------- 封面/歌词源

def _netease_songs(term: str, limit: int = 10) -> list[dict]:
    """跑一次 cloudsearch，返回原始 songs 列表。"""
    q = urllib.parse.quote(term)
    d = http_json(f"https://music.163.com/api/cloudsearch/pc"
                  f"?s={q}&type=1&offset=0&limit={limit}")
    return (d.get("result") or {}).get("songs") or []


def _netease_query_terms(artist: str, title: str) -> list[str]:
    """给网易云用的查询词，从最具体到最宽松。

    为什么要不止一个：文件名常常带着英文括注（`希望有羽毛和翅膀 (Hope Is the
    Thing With Feathers)`），拿整个名字去搜会命中云端那条 `al` 为空的残缺
    记录（图只有 240x240），而**去掉括注**搜同一条歌就能拿到带完整专辑的
    记录（3000x3000）。见 netease_search 的 docstring。
    """
    terms = [f"{artist} {title}".strip(), title.strip()]
    core = title_core(title).strip()
    if core and core not in terms:
        terms.append(core)
    out = []
    for t in terms:
        if t and t not in out:
            out.append(t)
    return out


# 网易云「这首歌没有封面」时给的默认图。实测命中它时图是纯色占位，
# 尺寸也可能过关，只能按 hash 直接认出来（见 netease_search）。
_PLACEHOLDER_PICS = ("109951162778086068",)


def netease_search(artist: str, title: str, limit: int = 10) -> list[tuple[float, dict]]:
    """网易云搜索，返回**按匹配度排序**的 [(分数, 候选)]。

    **必须用 cloudsearch 而不是 api/search/get**：后者对同 IP 限得极死，
    连打几十次就永久 405「操作频繁」，实测挂在那里十分钟都不恢复。
    cloudsearch 是网页版在用的接口，字段更全（ar/al 直接给歌手和专辑），
    实测连续打 6 次全部 code=200。
    顺带好处：al.picUrl 就是专辑封面，省掉原来"查专辑详情"的第二次请求。

    排序而不是过滤：网易云常把正主挤到后面（搜「晴天 周杰伦」前 5 条里
    真歌手写作 "Jay"），硬过滤会把对的条目一起扔掉。

    **准入：歌名过 TITLE_MIN，且歌手过 ARTIST_MIN**（后者经 artist_ok，
    能认别名表里的本名/艺名/罗马音）。歌手是硬门槛不能放宽 —— 实测整库
    歌手相似度是双峰分布，0.6-0.9 之间**一个都没有**，那 32 首低分的
    **全是翻唱版**；一旦放宽（试过"歌名一字不差或歌手对得上"），
    13 首翻唱立刻回归。

    **多查询合并（关键）**：网易云对同一首歌常常存在**两条记录**，一条挂在
    完整专辑上、另一条 `al` 全空（`id` 不同）。实测：
        查询「希望有羽毛和翅膀」                    -> 专辑=崩坏星穹铁道-空气蛹 INSIDE，图 3000x3000
        查询「知更鸟、HOYO-MiX、Chevy 希望有羽毛和翅膀 (Hope Is the Thing With Feathers)」
                                                    -> 专辑=空，图只有 240x240
    而文件名里带英文括注的正是后者，于是拿到一张缩略图。注意那张 240x240
    **不是默认占位图**（默认图的 hash 是 109951162778086068，对不上），
    是服务端对残缺记录真的只给这么大 —— 所以只能靠**换查询词**绕开，
    不能靠判图。做法：先按 `歌手 歌名` 查，再按 `歌名` 单独查一次，
    两批按 `id` 合并，**同 id 保留 al 信息更全的那条**；另外标题里
    带括号的部分（`(Live)`、`(Hope Is the Thing…)`）在第二次查询时剥掉。

    **专辑名同名加分**：网上到处是厂牌合辑（实测 `Lost Sky - Vision` 的
    第 1 条挂在专辑 `Top 50 NCS` 上，封面是 NCS 厂牌通用图；`Willow Tree`
    同样有一条 `Top 50 NCS`）。这类厂牌图的尺寸、体积、正方形判定**全部
    合格**，只能靠排序躲开。判别信号是「专辑名 == 歌名」—— 单曲/同名专辑
    才这样，厂牌合辑不会。

    **版本名降权**：同一首歌在网易云上挂着一堆翻版（`晴天 (钢琴版)`、
    `Hello (Workout Mix)`、`孤勇者 (童声版)`、`Brave Shine (TV size)`）。
    它们的歌名相似度同样过线、歌手分可能一样，封面却是别人的。实测这些
    翻版的图**往往比正主还大**（新上传的高清图），所以绝对不能按图的
    大小挑 —— 名字里的版本标记才是可靠信号，命中就重罚。
    """
    # 多查询合并：同一条云端记录在不同查询词下返回的 al 完整度不同，
    # 按 id 去重并保留 al 更全的那条（见上面 docstring 的实测）。
    merged: dict[int, dict] = {}
    for term in _netease_query_terms(artist, title):
        try:
            songs = _netease_songs(term, limit)
        except Exception:
            continue                  # 单个查询词失败不影响其他词
        for s in songs:
            sid = s.get("id")
            old = merged.get(sid)
            if old is None:
                merged[sid] = s
                continue
            def _rich(x):
                al = x.get("al") or {}
                return (1 if (al.get("name") or "").strip() else 0,
                        len(al.get("picUrl") or ""))
            if _rich(s) > _rich(old):
                merged[sid] = s

    scored = []
    for s in merged.values():
        got_name = s.get("name") or ""
        if looks_like_tribute(got_name):   # 翻唱/伴奏版，不是原曲
            continue
        got_artist = "、".join(a.get("name", "") for a in (s.get("ar") or []))
        if (s.get("al") or {}).get("picUrl") in _PLACEHOLDER_PICS:
            continue
        ts = title_similar(title, got_name)
        if ts < TITLE_MIN:            # 歌名不过线，歌手再像也不看
            continue
        a_sim = artist_similar(artist, got_artist)
        if not artist_ok(artist, got_artist):   # 歌手对不上 = 翻唱版/别人，歌名再像也不看
            continue
        score = 2.0 * ts + 1.0 * a_sim
        # 候选比我们查的人"多挂了几个人" → 多半是翻奏合辑，降权（见常量注释）
        extra = _artist_part_count(got_artist) - _artist_part_count(artist)
        if extra > 0:
            score -= COLLAB_PENALTY * extra
        if version_tagged(title, got_name):
            score -= VERSION_PENALTY
        album = ((s.get("al") or {}).get("name") or "").strip()
        # 加分要求**专辑名正好等于这首歌本身**（`norm(album) == norm(title)`），
        # 不能写成 `norm(album) == norm(got_name)` —— 那样翻版会把加分白拿走：
        # 实测 `Aimer - Brave Shine` 的候选里，「Amy B / Brave Shine (From
        # Fate/Stay Night)」的专辑正好同名，于是歌名分 0.80（"一方包含另一方"
        # 那档）+1.5 加分 = 3.64，**把正主 Aimer 的 3.00 顶了下去**。
        # `AGA - Wonderful U (Demo Version)` <- 「Zkaaai / Wonderful U」同理。
        # 而且必须只在**严格正方形**上给（见下面 pick），免得加分把长方形带进来。
        if album and norm(album) == norm(title):
            score += ALBUM_SAME_BONUS
        scored.append((score, s))
    scored.sort(key=lambda p: -p[0])
    return [(sc, s) for sc, s in scored if sc >= MATCH_THRESHOLD]


def _cover_variants(pic: str) -> list[bytes]:
    """同一张专辑图的几个可用版本（原图 + 各档方形裁切）。

    网易云的原图 URL 可以带 `?param=WxH` 让服务端**裁成正方形**，这是拿到
    合格封面的关键：很多专辑原图是长方形的（实测 `方大同 - 麦恩莉` 原图
    1535x1425），而 `?param=800y800` 回来的正是 800x800 正方形。

    调用方按面积挑最大的那个，所以这里只管把能取到的都取回来。
    """
    out: list[bytes] = []
    for url in (pic, f"{pic}?param=2000y2000", f"{pic}?param=1000y1000",
                f"{pic}?param=800y800", f"{pic}?param=500y500"):
        try:
            data = http_get(url)
        except Exception:
            continue                      # 某一档取不到不是错误，试下一档
        if data and data not in out:
            out.append(data)
    return out


def cover_from_netease(artist: str, title: str) -> tuple[bytes | None, str, str]:
    """网易云：搜索 -> 取匹配度最高的专辑封面。返回 (图, 标记, 状态)

    逐候选逐尺寸地找第一张合格的图，**只有整个候选都找不到合格图才换下一个
    候选**。这里必须按「候选」而不是按「图」来兜底：同样是「麦恩莉」，
    第一名是 `[方大同] 专辑=回到未来`（正确答案，原图非正方形），第二名是
    `[煤气灯] 专辑=Last piece`（**同名但完全不同的歌手**）。如果因为原图
    不合格就跳到下一候选，就会把别人的封面贴上去。

    合格候选**不止一个**时挑最大的那张，而不是第一个合格的。实测
    `Ariana Grande - Quit` 前几名是同一首歌的不同版本，第 1 名只有
    500x500、第 2 名有 1000x1000。

    **但不是无条件挑最大**：曾经按面积选过一轮，结果系统性偏向新上传的
    翻版（`Hello (Workout Mix)` 3000x3000 压过正主）。所以只有**分数并列
    第一**的候选之间才比大小，低分的候选再大也不越位。
    """
    ranked = netease_search(artist, title)
    # (分数, 面积, 图, 标记, 是否翻版)。分数跟着走，不能靠下标去并两个
    # 长度可能不同的列表 —— 取不到图的候选不会进 picks，下标就对不上了。
    picks: list[tuple[float, int, bytes, str, bool]] = []
    for score, s in ranked:
        pic = ((s.get("al") or {}).get("picUrl") or "").strip()
        if not pic:
            continue
        best_here: bytes | None = None
        for data in _cover_variants(pic):
            if len(data) > MAX_COVER_BYTES:      # 这一档太大，试下一档
                continue
            if cover_too_small(data):            # 这一档是缩略图/非方形，试下一档
                continue
            if best_here is None or _pixel_area(data) > _pixel_area(best_here):
                best_here = data
        if best_here is not None:
            picks.append((score, _pixel_area(best_here), best_here,
                          f"netease/{s.get('name')}",
                          version_tagged(title, s.get("name") or "")))
    if not picks:
        return None, "", MISS

    # 名次优先：按分数从高到低，取第一张合格的。
    # **同分取大**：实测 `Taylor Swift - Welcome To New York` 的同名单曲和
    # `1989 (Deluxe)` 都是 4.5 分，前者只给 1440、后者有 3200 —— 分数一样
    # 就没有理由拿小的。两个限制：
    #   - 只比**正方形**。面积对长方形天然有利，实测 `晴天` 的同分候选里
    #     混着一张 1080x1082（只差 0.2%，过了 cover_too_small 的宽严线），
    #     比面积它必然赢，结果就是给封面配一张非正方形图。
    #   - 翻版候选不参与（实测 `Quit (Wild Kidz Remix)` 那张只有 500，
    #     同分也轮不到它）。
    top_score = picks[0][0]
    same_rank = [(area, data, tag) for sc, area, data, tag, tagged in picks
                 if sc >= top_score and not tagged and _is_square(data)]
    if same_rank:
        _area, data, tag = max(same_rank, key=lambda p: p[0])
        return data, tag, OK
    return picks[0][2], picks[0][3], OK


def cover_from_itunes(artist: str, title: str) -> tuple[bytes | None, str, str]:
    q = urllib.parse.quote(f"{artist} {title}")
    d = http_json(f"https://itunes.apple.com/search?term={q}&entity=song&limit=10",
                  referer="https://itunes.apple.com/")
    scored = []
    for r in d.get("results") or []:
        got_name = r.get("trackName") or ""
        if looks_like_tribute(got_name):   # 翻唱/伴奏版，不是原曲
            continue
        ts = title_similar(title, got_name)
        if ts < TITLE_MIN:            # 同网易云：歌名先过线
            continue
        a_sim = artist_similar(artist, r.get("artistName") or "")
        if not artist_ok(artist, r.get("artistName") or ""):   # 同网易云：歌手必须对得上
            continue
        score = 2.0 * ts + 1.0 * a_sim
        # 同网易云：候选比我们查的人多挂几个 → 多半是翻奏合辑，降权
        extra = _artist_part_count(r.get("artistName") or "") - _artist_part_count(artist)
        if extra > 0:
            score -= COLLAB_PENALTY * extra
        if score >= MATCH_THRESHOLD:
            scored.append((score, r))
    scored.sort(key=lambda p: -p[0])
    for _sc, r in scored:
        raw = r.get("artworkUrl100") or ""
        if not raw:
            continue
        # artworkUrl100 里那段尺寸不一定写死成 "100x100"（见过 60x60、170x170…），
        # 所以按 \d+x\d+ 通配替换，而不是只 replace("100x100")。
        # 600x600 在体积（~100 KB）和清晰度之间比较平衡；单个不超上限。
        u = re.sub(r"\d+x\d+", "600x600", raw)
        if u == raw:                      # 没有尺寸段就退回原地址
            u = raw
        data = http_get(u, referer="https://itunes.apple.com/")
        if len(data) > MAX_COVER_BYTES:   # 太大就换小一档
            small = re.sub(r"\d+x\d+", "300x300", raw)
            if small != raw:
                alt = http_get(small, referer="https://itunes.apple.com/")
                if len(alt) < len(data):
                    data = alt
        if cover_too_small(data):         # 缩略图，换下一个候选
            continue
        return data, f"itunes/{r.get('trackName')}", OK
    return None, "", MISS


def lyrics_from_lrclib(artist: str, title: str) -> tuple[str | None, str, str]:
    """LRCLIB（lrclib.net）—— 免注册、基本不限流，且返回**带时间轴的 LRC**。

    为什么放第一位：网易云对同 IP 的搜索接口限得太狠（搜索/歌词都要走它），
    一首歌两次请求，跑几十首就被 405 挡住。LRCLIB 只按歌手+歌名查，
    一次请求拿到结果，中文库偏薄但英文/日文覆盖很好，正好互补。
    """
    import urllib.parse as _up
    url = "https://lrclib.net/api/search?" + _up.urlencode(
        {"artist_name": artist, "track_name": title})
    # LRCLIB 偶尔回 503（服务端忙）。它不限流、重试代价低，值得退避重试几次；
    # 不重试就会白跑一次网易云，白白消耗那点很宝贵的额度。
    rows = None
    last_exc = None
    for attempt, delay in enumerate((0.0, 2.0, 6.0)):
        if delay:
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                rows = json.loads(r.read().decode("utf-8", "replace"))
            break
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise                      # 404 之类是真的没有，不浪费时间重试
        except Exception as exc:
            last_exc = exc
    if rows is None:
        raise last_exc if last_exc else RuntimeError("LRCLIB 无响应")
    if not isinstance(rows, list) or not rows:
        return None, "", MISS
    # 按歌名+歌手综合打分挑，而不是"歌手对上就用第一条"：
    # LRCLIB 的模糊匹配会把 Live/翻唱版排在正版前面，而版本说明已经从
    # 歌名里剥掉了，所以必须再看歌手。
    best = None
    best_score = 0.0
    for row in rows:
        score = (title_similar(title, row.get("trackName") or "")
                 + 0.7 * artist_similar(artist, row.get("artistName") or ""))
        if score > best_score:
            best, best_score = row, score
    if best is None:                      # 歌手歌名都没对上，退回第一条
        best = rows[0]
    # 有带时间轴的优先（能在播放器里滚动歌词），没有才退回纯文本
    text = ((best.get("syncedLyrics") or "").strip()
            or (best.get("plainLyrics") or "").strip())
    if not text:
        return None, "", MISS
    return text, f"lrclib/{best.get('trackName')}", OK


def lyrics_from_netease(artist: str, title: str) -> tuple[str | None, str, str]:
    for _score, s in netease_search(artist, title):
        try:
            lrc = http_json(f"https://music.163.com/api/song/lyric"
                            f"?id={s['id']}&lv=1&kv=1&tv=-1")
        except RateLimited:
            raise
        except Exception:
            continue
        text = (lrc.get("lrc") or {}).get("lyric") or ""
        if text.strip():
            return text, f"netease/{s.get('name')}", OK
    return None, "", MISS


def get_cover_multi(cands: list[tuple[str, str]], verbose: bool = True):
    """按 name_pair 给的顺序逐个试，第一个交出合格封面就返回。

    只有「所有候选都确实没这首」（MISS）才报 MISS；只要有一个候选是
    限流/网络故障（BUSY），整体就算 BUSY —— 免得把"没查到"当成"没有"。
    返回 4 元组，第 4 个是命中用的那对 (歌手, 歌名)（没命中则 None），
    写标签时要用它，保证 src 记的是真正生效的那条查询。
    """
    busy = False
    for i, (artist, title) in enumerate(cands):
        c, s, st = get_cover(artist, title)
        if st == OK:
            if i and verbose:
                print(f"      换用另一套名字命中: {artist} - {title}")
            return c, s, OK, (artist, title)
        if st == BUSY:
            busy = True
    return None, "", BUSY if busy else MISS, None


def get_lyrics_multi(cands: list[tuple[str, str]], netease_fallback: bool = True):
    """同 get_cover_multi：逐个候选试歌词，返回 4 元组含命中的那对名字。"""
    busy = False
    for artist, title in cands:
        text, label, st = get_lyrics(artist, title, netease_fallback=netease_fallback)
        if st == OK:
            return text, label, OK, (artist, title)
        if st == BUSY:
            busy = True
    return None, "", BUSY if busy else MISS, None


def get_cover(artist: str, title: str) -> tuple[bytes | None, str, str]:
    """网易云优先，限流或没命中就退 iTunes。

    两个源拿回来的图都要过一遍「是不是占位图」：
      - 尺寸过小（缩略图）
      - **纯色**（网易云对没有封面的专辑返回一张纯黑图，尺寸体积都正常）
    两道都要过，否则换下一个源。
    """
    netease_status = MISS
    if _cooldown_left() <= 0:          # 冷却中就干脆不试，省得白挨一次 405
        try:
            c, s, st = cover_from_netease(artist, title)
            if st == OK:
                if cover_too_small(c):
                    print(f"      网易云给的是缩略图 {image_dims(c)}，换源")
                elif cover_is_flat(c):
                    print(f"      网易云给的是纯色占位图 {image_dims(c)}，换源")
                else:
                    return c, s, OK
                netease_status = MISS
            else:
                netease_status = st
        except RateLimited:
            netease_status = BUSY
            _enter_cooldown(COOLDOWN_BASE)
        except Exception as exc:
            print(f"      网易云取图异常: {type(exc).__name__} {exc}")
            netease_status = BUSY

    try:
        c, s, st = cover_from_itunes(artist, title)
        if st == OK:
            if cover_is_flat(c):
                print(f"      iTunes 给的也是纯色占位图 {image_dims(c)}，放弃配图")
                return None, "", MISS
            return c, s, OK
        # 网易云是忙的、iTunes 是真没有 -> 整体算「忙」，下次再试
        return None, "", BUSY if netease_status == BUSY else MISS
    except Exception as exc:
        print(f"      iTunes 取图异常: {type(exc).__name__} {exc}")
        return None, "", BUSY


def get_lyrics(artist: str, title: str, netease_fallback: bool = True) -> tuple[str | None, str, str]:
    """LRCLIB 优先（不限流、带时间轴），没命中再问网易云（中文库更全）。

    netease_fallback=False 时不查网易云 —— 跑整库时用它，免得一次限流之后
    还要为每首英文歌多花一次额度、多等一轮冷却。单曲调试时保持默认 True。
    """
    lrclib_status = MISS
    try:
        text, label, st = lyrics_from_lrclib(artist, title)
        if st == OK:
            return text, label, OK
        lrclib_status = st
    except Exception as exc:
        print(f"      LRCLIB 异常: {type(exc).__name__} {exc}")
        lrclib_status = BUSY

    if not netease_fallback:
        return None, "", lrclib_status

    if _cooldown_left() > 0:           # 冷却中就别再打了
        return None, "", BUSY
    try:
        text, label, st = lyrics_from_netease(artist, title)
        if st == OK:
            return text, label, OK
        return None, "", BUSY if lrclib_status == BUSY else MISS
    except RateLimited:
        _enter_cooldown(COOLDOWN_BASE)
        # 网易云被限流不算「这首歌没歌词」——LRCLIB 只是没命中而已，
        # 下次重跑网易云恢复后还能补上。
        return None, "", BUSY
    except Exception as exc:
        print(f"      网易云歌词异常: {type(exc).__name__} {exc}")
        return None, "", BUSY


# --------------------------------------------------------------------- 读写

def open_audio(path: str):
    """按**文件头**选解析器，不看扩展名。"""
    with open(path, "rb") as fh:
        magic = fh.read(8)
    if magic[:4] == b"fLaC":
        return FLAC(path), "flac"
    if magic[:4] == b"OggS":
        return OggVorbis(path), "ogg"
    if len(magic) >= 8 and magic[4:8] == b"ftyp":
        return MP4(path), "mp4"
    if magic[:3] == b"ID3" or magic[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        try:
            return ID3(path), "mp3"
        except ID3NoHeaderError:
            return ID3(), "mp3"
    return None, "?"


def _tidy_artist(name: str) -> str:
    """歌手名整理：去重 + 去掉「无畏契约」这类**不是歌手**的出品方名。

    实测库里的标签是 `Barns Courtney/ARB4/Eytan Peled/无畏契约`（无畏契约 =
    游戏《Valorant》的中文名，是出品方不是歌手）。它挂在歌手字段里会把
    歌手分拉低，本来能配上的封面就配不上了。
    """
    parts, seen = [], set()
    for p in re.split(r"[、/,;&:]", name or ""):
        p = p.strip()
        k = norm(p)
        if not p or not k or k in seen:
            continue
        if p in _NOT_ARTIST:
            continue
        seen.add(k)
        parts.append(p)
    return "、".join(parts)


# 出品方/游戏名，会混进歌手字段但不是歌手。只增不改，每条都要有实测依据。
_NOT_ARTIST = {"无畏契约", "VALORANT", "拳头游戏", "Riot Games"}


def name_pair(path: str, stem: str) -> list[tuple[str, str]]:
    """给一个文件，列出可以拿去搜索的 (歌手, 歌名) 组合，按可信度排序。

    文件名和标签各出**一对，交叉组合**（歌名一个来源、歌手另一个来源也常见），
    最后一个候选是"文件名歌名 + 标签歌手"。去重后返回。
    """
    fa, ft = split_stem(stem)
    ta, tt = tag_names(path)
    out, seen = [], set()
    for a, t in (
        (_tidy_artist(fa), ft),      # 原始行为，放第一，保证不改已有结果
        (_tidy_artist(ta), tt),      # 标签那套名字
        (_tidy_artist(fa), tt),      # 交叉
        (_tidy_artist(ta), ft),
    ):
        a, t = (a or "").strip(), (t or "").strip()
        if not a or not t:
            continue
        key = (norm(a), norm(t))
        if key in seen:
            continue
        seen.add(key)
        out.append((a, t))
    return out


def tag_names(path: str) -> tuple[str, str]:
    """读文件**自身标签**里的 (歌手, 歌名)，取不到返回空串。

    为什么要有这个：文件名和标签经常一个中文一个英文。实测
        SawanoHiroyuki[nZk]、Tielle - アメイジングツリーズ.flac
    文件名是日文原名「アメイジングツリーズ」，而网易云收的是英文名
    「Amazing Trees」—— 按文件名查一条都搜不到，按标签查第一条就命中。
    两套名字都试一遍，命中率明显更高。
    """
    try:
        f, kind = open_audio(path)
        if f is None:
            return "", ""
        t = f.tags or {}
        artist = title = ""
        if kind in ("flac", "ogg"):
            a = t.get("artist") or t.get("ARTIST")
            ti = t.get("title") or t.get("TITLE")
            artist = (a[0] if isinstance(a, list) and a else a) or ""
            title = (ti[0] if isinstance(ti, list) and ti else ti) or ""
        elif kind == "mp3":
            artist = str(t.get("TPE1") or "")
            title = str(t.get("TIT2") or "")
        elif kind == "mp4":
            artist = str(t.get("\xa9ART") or "")
            title = str(t.get("\xa9nam") or "")
        return str(artist).strip(), str(title).strip()
    except Exception:
        return "", ""


def has_cover(path: str) -> bool:
    f, kind = open_audio(path)
    if f is None:
        return False
    if kind == "flac":
        return bool(f.pictures)
    if kind == "mp3":
        return any(k.startswith("APIC") for k in f.keys())
    if kind == "mp4":
        return bool(f.tags and f.tags.get("covr"))
    if kind == "ogg":
        return bool(f.get("metadata_block_picture"))
    return False


# ------------------------------------------------------- 「查过了，没有」标记
#
# 为什么需要：曲库里有 88 首在 LRCLIB 和网易云**都没有歌词**（纯音乐、游戏
# 原声集、冷门 Remix）。旧行为是每轮流水线都把这 88 首重新问一遍 —— 实测
# 单首 miss 要 1 秒，网易云限流/LRCLIB 抽风时更要 37 秒，等于每次同步都白
# 等一分半到几十分钟。而这两件事的答案几个月都不会变。
#
# 所以把「查过了没有」写进文件的 comment 标签里，下次直接跳过：
#   MUSICFLOW_STATE=pic:miss:1750000000;lrc:miss:1750000000
# 过 RETRY_MISS_DAYS 天后再问一次（源会补数据），也可以命令行强制重试。
STATE_PREFIX = "MUSICFLOW_STATE="
RETRY_MISS_DAYS = 30.0

# 状态文件的默认位置（本次修复新增，2026-10-15）
#
# 为什么不能继续把状态写进音频文件的 comment
# ------------------------------------------
# 曲库是 Syncthing 的 sendonly 文件夹：**只要文件被写一次**（哪怕内容一个
# 字节没变），Syncthing 就重新算哈希、把这个文件整个重传给手机。而
# 「查过了没有」这个标记会随着时间戳（`lrc:miss:1791992507`）**每轮都变**，
# 于是每轮都要改一次 comment —— 改 comment 就得重写整个 40~160 MB 的容器。
# 实测日志里那行
#     `没有这首 -> 记下，30 天内不再问 (MUSICFLOW_STATE=lrc:miss:…;pic:miss:…)`
# 就是它在改动音频本体，一次能标脏上百首歌。
#
# 状态本来就不是音乐的一部分，没理由住在音频文件里。现在写到这个独立
# JSON；**读的时候仍然读文件标签**（老标记继续生效，不需要为了搬家而
# 回写任何一种文件）。
DEFAULT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "covers_state.json")

_ext_state_cache: dict[str, dict[str, tuple[str, float, str]]] | None = None
_ext_state_path: str = ""
_ext_state_dirty = False


def load_ext_state(path: str) -> dict[str, dict[str, tuple[str, float, str]]]:
    """读外部状态文件，返回 {文件名: {字段: (结果, 时间戳, 源)}}。

    读不出来（文件不存在/坏了）就当空字典 —— 最坏结果是多问一遍接口，
    不会写坏任何东西。
    """
    global _ext_state_cache, _ext_state_path
    if _ext_state_cache is not None and _ext_state_path == path:
        return _ext_state_cache
    _ext_state_path = path
    data: dict[str, dict[str, tuple[str, float, str]]] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        for fn, fields in (raw.get("files") or {}).items():
            if not isinstance(fields, dict):
                continue
            one: dict[str, tuple[str, float, str]] = {}
            for k, v in fields.items():
                if isinstance(v, list) and len(v) >= 2:
                    src = str(v[2]) if len(v) > 2 else ""
                    try:
                        one[str(k)] = (str(v[0]), float(v[1]), src)
                    except (TypeError, ValueError):
                        continue
            if one:
                data[str(fn)] = one
    except FileNotFoundError:
        pass
    except Exception as exc:                       # noqa: BLE001
        print(f"  ! 状态文件读取失败（当作空）: {type(exc).__name__} {exc}")
    _ext_state_cache = data
    return data


def save_ext_state(path: str) -> None:
    """把外部状态写回磁盘（原子替换）。

    记录会一直保留（包括暂时从曲库消失的文件）：万一用户把专辑挪走再挪回来，
    原来的「查过了没有」结论还能直接用。整个文件也就几十 KB。
    """
    global _ext_state_dirty
    data = _ext_state_cache or {}
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    files: dict[str, dict[str, list]] = {}
    for fn, fields in data.items():
        files[fn] = {k: [v[0], v[1], v[2]] for k, v in sorted(fields.items())}
    payload = {"version": 1,
               "note": "封面/歌词「查过了没有」的状态。改这里不会动音频文件。",
               "files": files}
    fd, tmp = tempfile.mkstemp(suffix=".json", dir=d or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        _ext_state_dirty = False
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def _set_ext_state(fn: str, state: dict[str, tuple[str, float, str]]) -> None:
    """把某个文件的状态写进外部状态表（只在内存里，退出时统一存盘）。"""
    global _ext_state_dirty
    if _ext_state_cache is None:
        load_ext_state(_ext_state_path or DEFAULT_STATE_FILE)
    assert _ext_state_cache is not None
    if state:
        _ext_state_cache[fn] = dict(state)
    else:
        _ext_state_cache.pop(fn, None)
    _ext_state_dirty = True



def parse_state(comment: str) -> dict[str, tuple[str, float, str]]:
    """从 comment 里解出 {字段: (结果, 时间戳, 试过的源)}。认不出就返回空字典。

    第三段（源）是后加的：`lrc:miss:123:lrclib` 表示「只拿 LRCLIB 试过，没有」。
    这样以后开了网易云兜底，会重新试一遍，而不是傻等 30 天冷却。
    """
    out: dict[str, tuple[str, float, str]] = {}
    i = (comment or "").find(STATE_PREFIX)
    if i < 0:
        return out
    body = (comment or "")[i + len(STATE_PREFIX):].split()[0]
    for item in body.split(";"):
        parts = item.split(":")
        if len(parts) < 3:
            continue
        field, result, ts = parts[0], parts[1], parts[2]
        src = parts[3] if len(parts) > 3 else ""
        try:
            out[field] = (result, float(ts), src)
        except ValueError:
            continue
    return out


def format_state(state: dict[str, tuple[str, float, str]]) -> str:
    def one(k: str, v: tuple[str, float, str]) -> str:
        result, ts, src = v
        return f"{k}:{result}:{int(ts)}" + (f":{src}" if src else "")
    return STATE_PREFIX + ";".join(one(k, v) for k, v in sorted(state.items()))


def read_state(path: str) -> dict[str, tuple[str, float, str]]:
    """读「查过了没有」的状态：文件标签 + 外部状态文件，两边的并集。

    两边都要读（2026-10-15）：
      * 老版本把状态写在音频文件的 comment 里，那些标记还有效，不能丢；
      * 新版本写在外部 JSON 里，因为往音频文件里写标记本身就会让 Syncthing
        把整首歌重传给手机 —— 时间戳每轮都在变，等于每轮都要重写一遍文件。
    优先级：**文件标签优先**。它是「针对这个文件本身」写的，比按文件名索引的
    外部记录更准（比如文件被换过、名字被复用的情况）。
    """
    from_tags: dict[str, tuple[str, float, str]] = {}
    try:
        f, kind = open_audio(path)
        if f is not None:
            t = f.tags or {}
            raw = ""
            if kind in ("flac", "ogg"):
                c = t.get("comment") or t.get("COMMENT") or t.get("description")
                if isinstance(c, list):
                    c = " ".join(str(x) for x in c)
                raw = str(c or "")
            elif kind == "mp3":
                raw = " ".join(str(t.get(k) or "") for k in t.keys()
                               if str(k).startswith("COMM"))
            elif kind == "mp4":
                raw = str(t.get("\xa9cmt") or "")
            # 旧版本只写过 MUSICFLOW_COVER_SRC，顺带兼容一下
            if not ("MUSICFLOW_COVER_SRC=" in raw and STATE_PREFIX not in raw):
                from_tags = parse_state(raw)
    except Exception:
        from_tags = {}
    ext = load_ext_state(_ext_state_path) if _ext_state_path else {}
    merged = dict(ext.get(os.path.basename(path)) or {})
    merged.update(from_tags)
    return merged


def state_says_missing(state: dict[str, tuple[str, float, str]], field: str,
                       sources: str = "", default_tried: str = "",
                       retry_days: float = RETRY_MISS_DAYS) -> bool:
    """这个字段是否已被判定为「没有」，且还在重试冷却期内。

    `sources` 是这次准备用的源集合（如 "lrclib,netease"）。只要这次要问的源
    里**有当时没问过的**，就不算「查过了」——否则开了网易云兜底也救不回来。
    `default_tried` 用于兼容没写源的老标记（当作这些源都试过了）。
    """
    got = state.get(field)
    if not got:
        return False
    result, ts, tried = got
    if result != "miss":
        return False
    if not tried:
        tried = default_tried
    if sources:
        want = {s for s in sources.split(",") if s}
        had = {s for s in tried.split(",") if s}
        # 注意：这里不能用 `if want - had:` —— 空集合在 Python 里是假值，
        # `had` 为空时 `want - had` 恒非空，等于永远不跳过。踩过一次。
        if not want.issubset(had):      # 有新源没试过 -> 值得再问一次
            return False
    age = time.time() - ts
    if age < -86400.0:          # 时间戳来自未来（时钟跳过/被改过）-> 当作没记过
        return False
    return age < retry_days * 86400.0


def has_lyrics(path: str) -> bool:
    f, kind = open_audio(path)
    if f is None:
        return False
    if kind == "flac":
        return bool(f.tags and ("lyrics" in f.tags or "unsyncedlyrics" in f.tags))
    if kind == "mp3":
        return any(k.startswith("USLT") for k in f.keys())
    if kind == "mp4":
        return bool(f.tags and f.tags.get("\xa9lyr"))
    if kind == "ogg":
        return bool(f.get("lyrics"))
    return False


def _comment_text(src: str, state: str) -> str:
    """comment 标签要同时装两样东西，拼成一行，别互相覆盖。

    实测踩过：封面 src 和状态标记都往 comment 写，后写的把先写的整个冲掉。
    """
    parts = []
    if src:
        parts.append(f"MUSICFLOW_COVER_SRC={src}")
    if state:
        parts.append(state)
    return " ".join(parts)


def _files_equal(a: str, b: str, chunk: int = 1 << 20) -> bool:
    """按大小 + SHA256 分块比对两个文件。

    分块读，不把整个文件读进内存：一首 Hi-Res 有 40~160 MB，原来的
    `fh.read()` 两边各读一整份，一批几百首下来内存能顶到几个 GB。
    """
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        h1, h2 = hashlib.sha256(), hashlib.sha256()
        with open(a, "rb") as f1, open(b, "rb") as f2:
            while True:
                b1 = f1.read(chunk)
                b2 = f2.read(chunk)
                if not b1 and not b2:
                    break
                h1.update(b1)
                h2.update(b2)
        return h1.digest() == h2.digest()
    except OSError:
        return False


def _norm_text(v) -> str:
    """标签值归一化，用来比较「要写的」和「已有的」是不是一回事。"""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    return str(v).strip()


def tags_need_write(path: str, cover: bytes | None, lyrics: str | None,
                    artist: str, title: str) -> bool:
    """只看现有标签就能确定「没什么要写的」——跳过，连临时副本都不做。

    这是第 ⑦ 条的主要修复点（2026-10-15）。修之前，即使封面和歌词早就有了，
    每轮仍会把文件整个复制一份、改一遍、再比对、再替换 —— 只要有一丁点不同
    （比如 comment 里的状态时间戳换了），文件就被重写，Syncthing 就得重传。

    只有**能明确判定为「已经一样」**时才返回 False。判断不了（读不了标签、
    容器不认识）一律返回 True，交给后面那条字节比对去兜底 —— 这里绝不能
    出现「以为不用写，其实该写」。
    """
    try:
        f, kind = open_audio(path)
        if f is None:
            return True
        t = f.tags or {}
        if cover:
            if kind in ("flac", "ogg"):
                if not (f.pictures if hasattr(f, "pictures") else []):
                    return True
            elif kind == "mp3":
                if not t.getall("APIC"):
                    return True
            elif kind == "mp4":
                if not t.get("covr"):
                    return True
            else:
                return True
        if lyrics:
            if kind in ("flac", "ogg"):
                if _norm_text(t.get("lyrics")) != _norm_text(lyrics):
                    return True
            elif kind == "mp3":
                uslt = t.getall("USLT")
                if not uslt or _norm_text(uslt[0].text) != _norm_text(lyrics):
                    return True
            elif kind == "mp4":
                if _norm_text(t.get("\xa9lyr")) != _norm_text(lyrics):
                    return True
            else:
                return True
        if artist:
            key = {"flac": "artist", "ogg": "artist", "mp3": "TPE1",
                   "mp4": "\xa9ART"}.get(kind)
            if not key or _norm_text(t.get(key)) != _norm_text(artist):
                return True
        if title:
            key = {"flac": "title", "ogg": "title", "mp3": "TIT2",
                   "mp4": "\xa9nam"}.get(kind)
            if not key or _norm_text(t.get(key)) != _norm_text(title):
                return True
        return False
    except Exception:
        return True


def write_into(path: str, cover: bytes | None, lyrics: str | None, artist: str,
               title: str, src: str = "", state: str = "") -> bool:
    """在临时副本上改，内容真的变了才原子替换。返回是否真的改写了文件。

    注意：mutagen 的 `save(路径)` **不是"另存为"**，而是"就地改写那个路径对应的
    文件对象"——它会先去读目标文件的 FLAC 头（flac.py:863 `__check_header`），
    所以传一个刚创建的空临时文件必然报
    `error file said 4 bytes, read 0 bytes`。
    正确顺序是：复制原文件 -> 在副本上开 mutagen -> 原地 save -> 原子替换。

    为什么要比对字节（2026-10-14 加）
    --------------------------------
    曲库是 Syncthing 的 sendonly 文件夹，**只要文件被写一次**（哪怕内容
    和原来一模一样），Syncthing 就会重新算哈希并把这个文件整个重传给手机。
    一首 Hi-Res 40-160 MB，之前每轮都要重传好几个 GB，原因就是这里在
    「封面/歌词已经有了、STATE 也没变」的情况下仍然无条件替换了文件。

    所以现在：改完副本先和原文件比对，**一致就直接丢弃副本、原文件
    的 mtime 和 inode 都不动** —— Syncthing 不会把它排进重传队列。

    2026-10-15 再加一道前置闸门：`tags_need_write()` 先在**不复制文件**的
    情况下看一眼标签，能断定「没什么要写」就直接返回，连那 100 MB 的临时
    副本都不产生。两条兜底都在，判定错任何一边也只会退化成「多写一次」，
    不会漏写。
    """
    if not tags_need_write(path, cover, lyrics, artist, title):
        return False
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(path)[1], dir=d)
    os.close(fd)
    try:
        shutil.copy2(path, tmp)          # 先弄一份完整的副本
        _write_in_place(tmp, cover, lyrics, artist, title, src, state)
        if _files_equal(path, tmp):
            os.unlink(tmp)               # 内容没变 -> 原件一个字节都不动
            return False
        os.replace(tmp, path)            # 再原子换回来
        return True
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def _write_in_place(path: str, cover: bytes | None, lyrics: str | None, artist: str,
                    title: str, src: str = "", state: str = "") -> None:
    f, kind = open_audio(path)

    if kind == "flac":
        if cover:
            p = Picture()
            p.type = 3
            p.mime = "image/jpeg" if cover[:3] == b"\xff\xd8\xff" else "image/png"
            p.desc = "Cover"
            p.data = cover
            f.clear_pictures()
            f.add_picture(p)
        if lyrics:
            f["lyrics"] = lyrics
        if artist:
            f["artist"] = artist
        if title:
            f["title"] = title
        ctext = _comment_text(src, state)
        if ctext:
            # 记下封面取自哪条记录 + 「查过了没有」的状态，都塞进 comment
            f["comment"] = ctext

    elif kind == "mp3":
        if cover:
            f.delall("APIC")
            f.add(APIC(encoding=3, mime="image/jpeg" if cover[:3] == b"\xff\xd8\xff" else "image/png",
                       type=3, desc="Cover", data=cover))
        if lyrics:
            f.delall("USLT")
            f.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
        if artist:
            f.setall("TPE1", [TPE1(encoding=3, text=artist)])
        if title:
            f.setall("TIT2", [TIT2(encoding=3, text=title)])
        ctext = _comment_text(src, state)
        if ctext:
            f.delall("COMM")
            f.add(COMM(encoding=3, lang="eng", desc="", text=ctext))

    elif kind == "mp4":
        if cover:
            fmt = 13 if cover[:3] == b"\xff\xd8\xff" else 14  # 13=JPEG 14=PNG
            f.tags["covr"] = [MP4Cover(cover, imageformat=fmt)]
        if lyrics:
            f.tags["\xa9lyr"] = lyrics
        if artist:
            f.tags["\xa9ART"] = artist
        if title:
            f.tags["\xa9nam"] = title
        ctext = _comment_text(src, state)
        if ctext:
            f.tags["\xa9cmt"] = ctext

    elif kind == "ogg":
        if cover:
            import base64
            p = Picture()
            p.type = 3
            p.mime = "image/jpeg" if cover[:3] == b"\xff\xd8\xff" else "image/png"
            p.desc = "Cover"
            p.data = cover
            f["metadata_block_picture"] = [base64.b64encode(p.write()).decode("ascii")]
        if lyrics:
            f["lyrics"] = lyrics
        if artist:
            f["artist"] = artist
        if title:
            f["title"] = title
        ctext = _comment_text(src, state)
        if ctext:
            f["comment"] = ctext
    else:
        raise ValueError(f"不支持的容器: {kind}")

    f.save()   # 就地写这个（临时副本）文件


# --------------------------------------------------------------------- 主流程

def _external_lrc_path(audio_path: str) -> str:
    return os.path.splitext(audio_path)[0] + ".lrc"


def write_external_lrc(audio_path: str, lyrics: str) -> bool:
    """把歌词写成同名 .lrc，**不碰音频文件**。返回是否真的写了盘。

    这是第 ⑦ 条的兜底路径（2026-10-15）。正常情况下歌词是嵌进标签里的，
    嵌不进去（容器特殊、标签写失败、文件只读）的时候 —— 尤其是遇到
    「歌已经有了、只是歌词没写进去」这一类 —— 与其反复重写几十 MB 的音频
    去挤一个标签，不如在它旁边放一个几百字节的 .lrc。

    .lrc 是几乎所有播放器都认的歌词格式（手机端、Windows、foobar 都认），
    而且它只有几 KB：Syncthing 传它的代价几乎为零。

    这里刻意**不做格式转换** —— 取回来的歌词原文什么样就存什么样，
    以免把时间轴改坏。
    """
    p = _external_lrc_path(audio_path)
    data = (lyrics or "").replace("\r\n", "\n").strip("\n")
    if not data:
        return False
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                if fh.read().replace("\r\n", "\n").strip("\n") == data:
                    return False          # 已有同样内容 -> 不动，mtime 保持
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data + "\n")
        os.replace(tmp, p)
        return True
    except OSError as exc:
        print(f"  ! 外挂歌词写入失败 {os.path.basename(p)}: {exc}")
        if os.path.exists(p + ".tmp"):
            try:
                os.unlink(p + ".tmp")
            except OSError:
                pass
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--apply", action="store_true", help="真正写入（不加则只报告）")
    ap.add_argument("--force", action="store_true", help="已有封面也重新取")
    ap.add_argument("--lyrics", action="store_true", help="顺带补歌词")
    ap.add_argument("--netease-lyrics", action="store_true",
                    help="歌词也查网易云（中文更全，但会被限流拖慢；默认只走 LRCLIB）")
    ap.add_argument("--retry-miss", action="store_true",
                    help=f"无视「查过了没有」标记，重查一遍（默认 {RETRY_MISS_DAYS:.0f} 天内跳过）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 首（试跑用）")
    ap.add_argument("--only", default="", help="只处理这些 '歌手 - 歌名'（分号分隔）")
    ap.add_argument("--sleep", type=float, default=0.6, help="每首之间的间隔秒数，避免被限流")
    ap.add_argument("--max-per-run", type=int, default=0,
                    help="本次最多处理几首（0=不限）。接进流水线时用它兜住单次耗时")
    ap.add_argument("--quiet", action="store_true",
                    help="逐首明细不打，只输出汇总（流水线里用，避免刷屏）")
    ap.add_argument("--state-file", default=DEFAULT_STATE_FILE,
                    help="「查过了没有」的状态存这里（独立 JSON，不写进音频文件）")
    args = ap.parse_args()

    # 状态放磁盘，不放音频文件：写音频文件会让 Syncthing 把整首歌重传给手机。
    # 老文件里已经写着的状态照读不误（见 read_state），不需要为了搬家回写任何文件。
    load_ext_state(args.state_file)

    only = {s.strip() for s in args.only.split(";") if s.strip()} if args.only else None
    files = sorted(f for f in os.listdir(args.dir)
                   if os.path.splitext(f)[1].lower() in AUDIO_EXT)
    if only:
        files = [f for f in files if os.path.splitext(f)[0] in only]

    need_pic, need_lrc, have_pic, have_lrc, no_artist = [], [], 0, 0, []
    states: dict[str, dict[str, tuple[str, float, str]]] = {}
    skipped_pic = skipped_lrc = 0
    for fn in files:
        p = os.path.join(args.dir, fn)
        artist, title = split_stem(os.path.splitext(fn)[0])
        if not artist:
            no_artist.append(fn)
            continue
        # 文件名一套名字、文件标签可能是另一套（一个日文一个英文很常见），
        # 两套都留着，取图时逐个试 —— 见 name_pair 的说明。
        cands = name_pair(p, os.path.splitext(fn)[0])
        try:
            hp, hl = has_cover(p), has_lyrics(p)
        except Exception as exc:
            print(f"  ! 读取失败 {fn}: {exc}")
            continue
        st = read_state(p)          # 「查过了没有」的标记，等下用来跳过重查
        states[fn] = st
        pic_srcs = "netease,itunes"
        lrc_srcs = "lrclib,netease" if args.netease_lyrics else "lrclib"
        if hp and not args.force:
            have_pic += 1
        elif args.retry_miss or not state_says_missing(
                st, "pic", pic_srcs, default_tried="netease,itunes"):
            need_pic.append((fn, cands))
        else:
            skipped_pic += 1
        if hl and not args.force:
            have_lrc += 1
        elif not args.lyrics:
            pass
        elif args.retry_miss or not state_says_missing(
                st, "lrc", lrc_srcs, default_tried="lrclib"):
            need_lrc.append((fn, cands))
        else:
            skipped_lrc += 1

    print(f"扫描 {args.dir}: {len(files)} 个音频文件")
    print(f"  已有封面 : {have_pic}")
    print(f"  缺封面   : {len(need_pic)}")
    if skipped_pic:
        print(f"  已查过没有(跳过): {skipped_pic}")
    if args.lyrics:
        print(f"  已有歌词 : {have_lrc}")
        print(f"  缺歌词   : {len(need_lrc)}")
        if skipped_lrc:
            print(f"  已查过没有(跳过): {skipped_lrc}")
    print(f"  文件名无歌手: {len(no_artist)}")
    print()

    # 待办 = 缺封面的 ∪ 缺歌词的。
    # 只补歌词的那些（封面已有）也要进队列，但取图那步会跳过，不浪费请求。
    need_pic_names = {fn for fn, _ in need_pic}
    need_lrc_names = {fn for fn, _ in need_lrc}
    todo = need_pic + [t for t in need_lrc if t[0] not in need_pic_names]
    todo.sort(key=lambda t: t[0].lower())
    cap = args.limit or args.max_per_run
    if cap:
        todo = todo[:cap]

    if not args.apply:
        print("[dry-run] 未修改。加 --apply 执行。")
        print(f"  本次会处理 {len(todo)} 首")
        for fn, _c in todo[:15]:
            print(f"    {fn}")
        return 0

    ok_pic = miss_pic = busy_pic = ok_lrc = miss_lrc = busy_lrc = 0
    unchanged_pic = unchanged_lrc = 0
    busy_streak = 0
    aborted = False
    def _log(msg: str) -> None:
        """始终打印（不受 --quiet 影响）—— 跳过和标记这类"我做了什么决定"
        必须留在日志里，否则用户从流水线看到的就是一片空白。"""
        print(f"  {msg}", flush=True)

    for i, (fn, cands) in enumerate(todo, 1):
        p = os.path.join(args.dir, fn)
        if not args.quiet:
            print(f"  [{i}/{len(todo)}] {fn}")
        cover = src = None
        lyr = None
        pic_status = lrc_status = None
        want_pic = fn in need_pic_names
        want_lrc = fn in need_lrc_names
        st = dict(states.get(fn) or {})
        old_pic = st.get("pic")
        old_lrc = st.get("lrc")
        # new_state 只喂给「本来就要写文件」的那些歌 —— 让文件里的标记和外部
        # 状态保持一致；**它本身绝不构成写文件的理由**（这曾是每次运行都把
        # 上百首歌标脏、重传几个 GB 的根源）。
        new_state = ""

        if want_pic:
            cover, src, pic_status, pic_name = get_cover_multi(cands, verbose=not args.quiet)
        else:
            pic_name = None

        if args.lyrics and want_lrc:
            lyr, lsrc, lrc_status, lrc_name = get_lyrics_multi(
                cands, netease_fallback=args.netease_lyrics)
        else:
            lrc_name = None

        # 写标签用**真正命中那条**的名字；都没命中就用首选那套（等于沿用旧行为）
        artist, title = pic_name or lrc_name or cands[0]

        # 把这一轮的「查过了没有」记进状态，下轮直接跳过，不再白花这一秒
        now = time.time()
        if want_pic and not cover and pic_status == MISS:
            st["pic"] = ("miss", now, pic_srcs)
        elif cover:
            st.pop("pic", None)
        if want_lrc and not lyr and lrc_status == MISS:
            st["lrc"] = ("miss", now, lrc_srcs)
        elif lyr:
            st.pop("lrc", None)
        state_changed = (st.get("pic") != old_pic) or (st.get("lrc") != old_lrc)
        if state_changed:
            _set_ext_state(fn, st)
            new_state = format_state(st) if st else ""

        if not cover and not lyr:
            # 三种情况分开计数 —— 「限流没取到」和「确实没有」必须能区分，
            # 否则限流一次就会被当成几百首「没有」。
            if pic_status == BUSY or lrc_status == BUSY:
                busy_pic += 1
                busy_streak += 1
                if not args.quiet:
                    print("      跳过：被限流/网络故障，下次运行会重试")
                if busy_streak >= ABORT_AFTER_BUSY:
                    print(f"      连续 {busy_streak} 首没取到，收工。"
                          f"剩下的留到下次运行（已经取到的不会重复取）。")
                    aborted = True
                    break
            else:
                miss_pic += 1
                busy_streak = 0
                if not args.quiet:
                    print("      跳过：接口正常但没有这首")
            # 确实没有 -> 落标记，下次不再问；限流则**不落标记**，下次还要重试。
            # 标记只写外部状态文件（上面 _set_ext_state 已经写了），**不再回写
            # 音频文件** —— 回写会让 Syncthing 把整首歌重新传给手机，而
            # 「我查过了没有」这个结论本来就不是音乐的一部分。
            if state_changed:
                _log(f"没有这首 -> 记下，{RETRY_MISS_DAYS:.0f} 天内不再问 ({new_state})")
            time.sleep(args.sleep)
            continue

        try:
            changed = write_into(p, cover, lyr, artist, title, src or "", new_state)
            if cover:
                if changed:
                    ok_pic += 1
                else:
                    unchanged_pic += 1
                if not args.quiet:
                    print(f"      ✓ 封面 {len(cover)//1024} KB  ({src})"
                          + ("" if changed else "  [已存在，未写盘]"))
            if lyr:
                if changed:
                    ok_lrc += 1
                else:
                    unchanged_lrc += 1
                if not args.quiet:
                    print(f"      ✓ 歌词 {len(lyr)} 字符"
                          + ("" if changed else "  [已存在，未写盘]"))
            busy_streak = 0
        except Exception as exc:
            # 嵌进标签失败：退一步写外挂 .lrc —— 几百字节，不动音频本体，
            # 总比为了塞歌词把整首 100 MB 的歌重写一遍强。
            printed = False
            if lyr:
                if write_external_lrc(p, lyr):
                    _log(f"! 标签写入失败，已改用外挂歌词 "
                         f"{os.path.basename(_external_lrc_path(p))}: "
                         f"{type(exc).__name__} {exc}")
                    ok_lrc += 1
                    printed = True
            if not printed:
                print(f"      ✗ 写入失败: {type(exc).__name__} {exc}")
            busy_pic += 1
            busy_streak += 1
        time.sleep(args.sleep)

    print()
    print(f"封面: 成功 {ok_pic}, 没有 {miss_pic}, 限流未取 {busy_pic}")
    if args.lyrics:
        print(f"歌词: 成功 {ok_lrc}")
    if unchanged_pic or unchanged_lrc:
        # 这些文件本来就已经有同样的封面/歌词，一个字节都没写，
        # Syncthing 因此不会把它们重新传给手机（这是省流的关键）
        print(f"内容未发生变化: 封面 {unchanged_pic} 首, 歌词 {unchanged_lrc} 首"
              f"（未写盘，不会触发同步重传）")
    if skipped_pic or skipped_lrc:
        print(f"  省下的重复查询: 封面 {skipped_pic} 首, 歌词 {skipped_lrc} 首"
              f"（之前查过确认没有，{RETRY_MISS_DAYS:.0f} 天内不问）")
    # 状态落盘（只在内存里攒着，最后写一次 —— 不会每首去动一次状态文件）
    try:
        save_ext_state(args.state_file)
    except Exception as exc:
        print(f"  ! 状态文件保存失败（不影响本次结果，下轮会重查）: "
              f"{type(exc).__name__} {exc}")
    if aborted:
        print("  提示：撞上网易云限流（同 IP 搜太多次）。隔十几分钟重跑即可，"
              "已经取到的不会重复取。")
        # 用非 0 退出码让流水线把这步标成「待处理」而不是「完成」
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
