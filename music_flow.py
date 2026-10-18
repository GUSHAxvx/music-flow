#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Music Flow —— 音乐下载/监测/同步全流程管家（后端）

设计目标
--------
把原本散落在多个脚本里的流程，包装成一个「过程透明」的应用：
  * 省心模式：连上热点后点一下「同步」，全流程自动跑完
  * 观察模式：每一个步骤都能单独点击、单独运行、单独看日志

核心设计：**两种模式共用同一份流水线定义**（PIPELINE）。
自动模式 = 按顺序把步骤全跑一遍；手动模式 = 同一个列表逐个点。
这样"自动跑的东西"和"手动点的东西"永远是同一套逻辑，不存在两套实现。

只依赖 Python 标准库（http.server / json / subprocess / threading），
不需要 pip install，不需要 Node，不额外占内存。

用法
----
    python music_flow.py            # 启动并自动打开浏览器
    python music_flow.py --no-open  # 不自动开浏览器
"""
import argparse
import ctypes
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
LOGS = os.path.join(BASE, "logs")
CONFIG = os.path.join(BASE, "config.json")
PORT = 8477

# 外部依赖（都是已经跑通的东西，这里只做调用）
MONITOR = r"D:\music-monitor"
DOCKER = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
FFPROBE = (r"C:\Users\Administrator\AppData\Local\Microsoft"
           r"\WinGet\Links\ffprobe.exe")
PY = sys.executable
PICKER = os.path.join(BASE, "scripts", "pick_files.ps1")


def _console_encoding():
    """子进程输出该按什么编码解。

    为什么不能写死 utf-8：music-decode 是 C++ 程序，用 Windows 的 OEM
    代码页（中文系统上是 cp936）往控制台写中文。按 utf-8 去解就得到
    `鍋囧姞瀵?flac` 这种乱码——日志里的报错信息全成了天书，事后根本
    没法排查。反过来，我们自己那些 python 子进程设了
    PYTHONIOENCODING=utf-8，输出的是 utf-8。所以先探测系统 OEM 页。
    """
    for api in ("GetOEMCP", "GetACP"):
        try:
            cp = getattr(ctypes.windll.kernel32, api)()
            if cp:
                return f"cp{cp}"
        except Exception:                      # noqa: BLE001
            pass
    return "utf-8"


OUT_ENC = _console_encoding()

# music-decode（加密音乐解码器）：把 ncm / qmc / kgm / vpr / kgg 还原成
# 普通的 flac / mp3 / ogg。这一步必须在「取样率闸门」之前，因为闸门要用
# ffprobe 读真实规格，而加密文件根本读不出来 —— 会被当成坏文件丢掉。
MUSIC_DECODE = r"D:\music-decode\build\music_decode.exe"
# 引擎暂存的宿主直连路径（library-manager.ps1 里的 $stagingDirect，
# 容器 /app/data/hires_staging 就 bind 挂载在这里）
HIRES_STAGING = os.path.join(MONITOR, "config", "monitor", "hires_staging")
# 本机暂存副本（library-manager.ps1 里的 $staging）
LOCAL_STAGING = os.path.join(MONITOR, "_staging")

# 加密音乐容器的扩展名。**这份清单必须和解码器的判定保持一致**
# （D:\music-decode\src\decoders\unified_decoder.cpp:581 detectProtectedAudioFormat）。
# 踩过的坑：我自己加了 .mgg/.mflac（那是 QQ 音乐另一种容器）、又漏了
# .flac —— 酷狗的「加密 FLAC」在磁盘上就叫 .flac，光看扩展名分不出来，
# 得读文件头。所以下面还配了一份「伪装成 flac 的加密文件」的头字节。
DECODE_EXT = (".ncm", ".qmc0", ".qmc3", ".qmcflac", ".qmcogg",
              ".kgm", ".kgma", ".vpr", ".kgg")
# 酷狗/酷我加密文件的文件头。普通 FLAC 前 4 字节是 "fLaC"，这些都不是，
# 所以能一眼分开。
#
# 为什么只比前 12 字节（踩过的坑）
# ---------------------------------
# 解码器源码里的 KGM_HEADER 是完整 16 字节，最后 4 字节写的是
# `0f f9 91 e4`。但实测磁盘上的文件是 `0f ff 99 14` —— 而且**35 个样本
# 里 35 个都是 `0fff9914`**，反倒是源码里那个常量一个都没对上。
# 结论：这 4 个字节是**跟文件走的数据**（酷狗换过一次密钥方案），
# 不是固定的魔数。照抄 16 字节常量的话，所有变体都会被判成「不是加密文件」
# 而静默跳过 —— 用户导 .kgma 失败就是这么来的。
# 所以这里只认前 12 字节这个真正稳定的部分。
KGM_HEADER = bytes([0x7c, 0xd5, 0x32, 0xeb, 0x86, 0x02, 0x7f, 0x4b,
                    0xa8, 0xaf, 0xa6, 0x8e])
VPR_HEADER = bytes([0x05, 0x28, 0xbc, 0x96, 0xe9, 0xe4, 0x5a, 0x43,
                    0x91, 0xaa, 0xbd, 0xd0])
# 酷狗这一族的统一特征：不管叫 .kgg / .kgma / .kgm / .kgtemp，
# 内容开头都是这 12 个字节，走的是同一个解码内核。
KGM_FAMILY_EXT = (".kgg", ".kgma", ".kgm", ".kgtemp")

# 纯 Python 的酷狗解密模块（scripts/kugou_kgm.py）。
# 为什么需要它：music-decode 的 C++ KGM 路径把文件头 16 字节写死成源码常量，
# 而真实文件那 4 字节是跟文件走的数据，于是整族被判「不支持」。这个模块
# 按「头部 0x1C 起的 16 字节逐文件密钥」真解，实测 .kgma/.kgm/.kgtemp 都能
# 完整还原（产物与可信基线逐字节一致）。
SCRIPT_DIR = os.path.join(BASE, "scripts")
_KGM_MOD = None


def _kgm_module():
    """懒加载 scripts/kugou_kgm.py；不可用返回 None（不抛）。"""
    global _KGM_MOD
    if _KGM_MOD is None:
        try:
            if SCRIPT_DIR not in sys.path:
                sys.path.insert(0, SCRIPT_DIR)
            import kugou_kgm as _m            # noqa: N816
            _KGM_MOD = _m
        except Exception:                      # noqa: BLE001
            _KGM_MOD = False                   # 记住失败，不反复 import
    return _KGM_MOD or None

# 手动导入模块（同目录）
_imp = None
try:
    import import_songs as _imp            # noqa: N816
except Exception:                          # noqa: BLE001
    _imp = None

# 曲库在容器内的挂载点（已确认 music-monitor 把 D:\go-music-dl\downloads
# 挂在 /downloads）。
# 为什么下载脚本要知道曲库：否则取流临时失败时会把一首**已经在库里的歌**
# 误加入屏蔽名单（踩过这个坑）。
CONTAINER_LIB = "/downloads"

os.makedirs(LOGS, exist_ok=True)


# ══════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════
DEFAULT_CONFIG = {
    "download": {
        "note": "决定哪些歌会被下载",
        "accept_hires": True,          # 接受 24bit Hi-Res
        "accept_lossless": True,       # 接受 16bit 无损
        "accept_lossy": False,         # 接受有损（128k/320k）—— 默认关闭，避免音质降级
        "only_new_lossless": True,     # 无损只跟新歌，不回填历史（严格模式）
        "max_albums": 400,             # 首次回填规模；有基线时该值不生效
        "lossless_target": 30,         # 无损目标数量（仅在 only_new_lossless 关闭时有意义）
        "sort_mode": "bitrate",        # bitrate=优先高码率 / newest=优先最新收藏
        "min_bitrate": 0,              # 低于此码率不下载（0=不限）
    },
    "guard": {
        "note": "决定哪些歌会被拦截，不进同步文件夹",
        "block_sample_rates": [176400],
        "block_channels": [],
        "block_reason_176400": "176.4kHz 在本机手机链路上会输出噪声，实测炸耳",
        "content_mismatch_blocklist": True,   # 尊重 blocked.txt 里的内容错误曲目
    },
    "import": {
        "note": "手动导入歌曲时的规则",
        # 手动导入默认允许有损：是你自己挑的文件，通常是有意为之。
        # 自动下载那条路仍然默认只收无损（download.accept_lossy）。
        "accept_lossy": True,
    },
    "sync": {
        "note": "同步到手机。device_id / api_key 是个人信息，请填自己的 —— 别提交进仓库。",
        "folder_id": "go-music-dl-downloads",
        # 以下三项请填自己的值。仓库里的 config.example.json 是空模板。
        "phone_device": "",
        "api_key": "",
        "syncthing_gui": "http://127.0.0.1:8384",
        "syncthing_exe": r"C:\Program Files\Syncthing\syncthing.exe",
        # 注意：这里**不要**写 shell 风格的引号。
        # Python 的 subprocess 不经过 shell，引号会被当成参数内容的一部分，
        # Syncthing 会因此去创建形如 "\"C:" 的怪目录（踩过这个坑）。
        "syncthing_args": [
            r"--config=C:\ProgramData\Syncthing\config",
            r"--data=C:\ProgramData\Syncthing\data",
            "--no-browser",
        ],
        "downloads_dir": r"D:\go-music-dl\downloads",
    },
    "docker": {
        "desktop_exe": r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
        "containers": ["go-music-dl", "music-monitor"],
    },
    "covers": {
        "note": "补齐封面/歌词。引擎走 POST 落盘路径时不会嵌封面，所以在本地补。",
        "enabled": True,
        "lyrics": True,
        # 歌词也问网易云。LRCLIB 的中文库偏薄（中文歌经常整个查不到），
        # 网易云中文全 —— 代价是会被限流，靠脚本自己的冷却兜住；
        # 而且查过的不会重复查（负结果写进文件标签，30 天才重试），
        # 所以只有新歌才花这份钱。关掉它=中文歌歌词会明显少一截。
        "netease_lyrics": True,
        # 单次运行最多处理多少首（防止第一次跑全库时把流水线拖太久；0=不限）
        "max_per_run": 60,
    },
    "decode": {
        "note": "解码加密音乐（ncm/qmc/kgm/vpr/kgg）。必须先解码，"
                "否则下一道采样率闸门用 ffprobe 读不出规格，会把它们当坏文件丢掉。",
        "enabled": True,
        "threads": 0,          # 0 = 让解码器自己决定（实测单线程 1548 MB/s，够用）
        "delete_source": True, # 校验通过后删掉加密原件
    },
    "advanced": {
        "note": "依赖的开源项目与脚本（一般不用改）",
        "library_manager": r"D:\music-monitor\library-manager.ps1",
        "manage_library_py": "/app/manage_library.py",
        "tag_files_py": r"D:\music-monitor\tag_files.py",
        "check_integrity_py": r"D:\music-monitor\check_integrity.py",
        "samplerate_guard_py": r"D:\music-monitor\samplerate_guard_host.py",
        "audit_quality_ps1": r"D:\music-monitor\audit_quality.ps1",
        "fix_covers_py": os.path.join(BASE, "scripts", "fix_covers.py"),
        "music_decode_exe": MUSIC_DECODE,
    },
}


def load_config():
    if not os.path.exists(CONFIG):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        return json.loads(json.dumps(DEFAULT_CONFIG))
    # 补齐缺失的键
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v

    # 迁移 1：老配置里 syncthing_args 带了 shell 风格引号，会让 Syncthing
    # 去创建形如 "\"C:" 的怪目录。这里统一去掉首尾引号。
    args = merged.get("sync", {}).get("syncthing_args")
    if isinstance(args, list):
        cleaned = []
        for a in args:
            if isinstance(a, str):
                a = a.strip()
                if a.startswith('"') and a.endswith('"'):
                    a = a[1:-1]
                elif a.startswith("'") and a.endswith("'"):
                    a = a[1:-1]
            cleaned.append(a)
        merged["sync"]["syncthing_args"] = cleaned

    return merged


def save_config(cfg):
    with open(CONFIG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════
#  事件总线（前端通过 SSE 订阅）
# ══════════════════════════════════════════════════════════════
class Bus:
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()
        self.recent = []            # 最近事件，供新连接的客户端补看

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
        for ev in self.recent[-200:]:
            q.put(ev)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self, etype, **data):
        ev = {"type": etype, "ts": datetime.now().strftime("%H:%M:%S"), **data}
        with self.lock:
            self.recent.append(ev)
            if len(self.recent) > 2000:
                self.recent = self.recent[-1000:]
            for q in self.clients:
                try:
                    q.put_nowait(ev)
                except Exception:
                    pass


BUS = Bus()


def log(msg, level="info", step=None):
    """记录一行日志。

    同时做三件事：
      1) 推给前端（实时显示）
      2) 写进本次运行的日志文件（事后排查）
      3) 打到终端（在没有前端的时候 —— 比如从命令行单独跑某一步）
    —— 只推前端不写文件的话，出问题后翻 run 日志会看不到任何线索；
       只推前端不打终端的话，命令行跑就变成「一片安静」，看起来像没跑。
    """
    text = str(msg)
    BUS.publish("log", message=text, level=level, step=step)
    try:
        RECORDER.write(f"[{level}] {text}" if level != "out" else text)
    except Exception:
        pass
    try:
        print(f"[{level}] {text}" if level != "out" else text, flush=True)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
#  运行记录（每次运行写一个日志文件，便于事后排查）
# ══════════════════════════════════════════════════════════════
class RunRecorder:
    def __init__(self):
        self.path = None
        self.fh = None
        self.lock = threading.Lock()

    def start(self, title):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = "".join(c for c in title if c.isalnum() or c in "-_")[:40]
        self.path = os.path.join(LOGS, f"{stamp}_{safe}.log")
        self.fh = open(self.path, "w", encoding="utf-8")
        self.write(f"===== {title}  开始于 {datetime.now():%Y-%m-%d %H:%M:%S} =====")
        return self.path

    def write(self, line):
        with self.lock:
            if self.fh:
                self.fh.write(line.rstrip() + "\n")
                self.fh.flush()

    def close(self, ok=None):
        if self.fh:
            tail = "结束" if ok is None else ("成功" if ok else "失败")
            self.write(f"===== {tail}  于 {datetime.now():%Y-%m-%d %H:%M:%S} =====")
            self.fh.close()
            self.fh = None


RECORDER = RunRecorder()


class _OutDecoder:
    """子进程输出解码器：先按 utf-8 解，不行再按系统 OEM 页解。

    为什么需要它：这一条流水线里的子进程有两类，编码正好相反
      · 我们自己起的 python 子进程设了 PYTHONIOENCODING=utf-8 → 输出 utf-8
      · music-decode 这类 C++ 程序写的是 utf-8 字节流，但 Windows 控制台
        是 cp936，直接按 cp936 解就得到 `鍋囧姞瀵?flac` 这种乱码
    写死任何一种都会让另一类变乱码，所以逐行探测。
    """

    def __init__(self, fallback):
        self.fallback = fallback
        self.pending = b""

    def feed(self, chunk: bytes):
        """喂一段字节，返回能确定的行（可能为空列表）"""
        self.pending += chunk
        lines = []
        while True:
            pos = self.pending.find(b"\n")
            if pos < 0:
                break
            raw, self.pending = self.pending[:pos], self.pending[pos + 1:]
            lines.append(self._decode(raw))
        return lines

    def flush(self):
        """收尾：把最后没有换行的一段也吐出来"""
        tail, self.pending = self.pending, b""
        return [self._decode(tail)] if tail else []

    def _decode(self, raw: bytes) -> str:
        for enc in ("utf-8", self.fallback):
            try:
                return raw.decode(enc).rstrip("\r")
            except UnicodeDecodeError:
                continue
        return raw.decode(self.fallback, errors="replace").rstrip("\r")


# ══════════════════════════════════════════════════════════════
#  命令执行 + 实时输出
# ══════════════════════════════════════════════════════════════
ERROR_HINTS = ("error", "failed", "失败", "错误", "异常", "traceback",
               "拒绝", "denied", "cannot", "no such", "找不到", "中止")
# 这些模式即使含「失败/错误」字样也不算错误
# 例：「补写完成: 成功 67, 失败 0」是在报告成功，标红会误导
ERROR_EXEMPT = (
    r"成功\s*\d+\s*[,，]\s*失败\s*0",      # 补写完成: 成功 67, 失败 0
    r"失败\s*[:：]\s*0\b",                 # 读取失败    : 0
    r"错误数\s*[:：]\s*0\b",
    r"失败\s*0\s*个",
    r"0\s*个失败",
    r"没有需要拦截",
    r"无需下载",
)


def looks_like_error(line):
    low = line.lower()
    if any(re.search(p, line) for p in ERROR_EXEMPT):
        return False
    return any(h in low for h in ERROR_HINTS)


def _fmt_dur(seconds):
    """把秒数写成一眼能看懂的样子（性能排查用）。"""
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    m, s = divmod(int(round(seconds)), 60)
    if m < 60:
        return f"{m} 分 {s} 秒"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分 {s} 秒"


# 当前正在运行的子进程 + 停止信号。
# 「停止」要真的能中断，而不是等当前步骤跑完 —— 否则用户只能干等。
_CURRENT_PROC = None
_STOP_EVENT = threading.Event()


class StepAborted(Exception):
    """步骤在中途看到停止请求，自己把收尾做完后抛出。

    为什么要专门一个异常类型，而不是让步骤 return False：
      return False 会被当成「这一步失败了」，日志里标红、还可能触发「自动模式
      出错即停止」的措辞，看起来像出了故障。中途停止是用户的正常操作，只是
      这一步没做完，得和失败区分开（运行器把它记成 stopped）。
    """


def kill_tree(pid):
    """杀掉进程及其所有子进程。

    为什么不能只用 p.kill()：PowerShell / docker 这类命令会派生子进程。
    实测：停止第 5 步后仍等了 80 秒才真正结束 —— 因为杀掉 PowerShell 父进程后，
    它派生的 docker cp / ffprobe 还在继续跑。必须连整棵树一起杀。
    """
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=20)
    except Exception:
        pass


def request_stop():
    _STOP_EVENT.set()
    p = _CURRENT_PROC
    if p and p.poll() is None:
        log(f"正在终止任务树（PID {p.pid}）…", "warn")
        kill_tree(p.pid)


def run_cmd(args, step=None, cwd=None, timeout=None, env=None):
    """执行命令并实时把输出推给前端。返回 (ok, 输出文本)"""
    global _CURRENT_PROC
    shown = " ".join(str(a) for a in args)
    log(f"$ {shown}", "cmd", step)
    RECORDER.write(f"$ {shown}")
    try:
        p = subprocess.Popen(
            args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, env=env or os.environ.copy())
    except FileNotFoundError as exc:
        msg = f"命令不存在: {args[0]}  ({exc})"
        log(msg, "error", step)
        return False, msg

    _CURRENT_PROC = p
    lines = []
    dec = _OutDecoder(OUT_ENC)
    start = time.time()
    killed = False
    try:
        while True:
            chunk = p.stdout.readline()
            if not chunk:
                break
            for line in dec.feed(chunk):
                lines.append(line)
                lvl = "error" if looks_like_error(line) else "out"
                log(line, lvl, step)
            if _STOP_EVENT.is_set():
                killed = True
                break
            if timeout and time.time() - start > timeout:
                p.kill()
                log("超时，已终止", "error", step)
                break
        for line in dec.flush():
            lines.append(line)
            lvl = "error" if looks_like_error(line) else "out"
            log(line, lvl, step)
            RECORDER.write(line)
        p.wait()
    finally:
        _CURRENT_PROC = None

    if killed:
        log("已被用户停止（当前命令已终止）", "warn", step)
        return False, "\n".join(lines)
    ok = (p.returncode == 0)
    if not ok:
        RECORDER.write(f"!! 退出码 {p.returncode}")
    return ok, "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  流水线步骤定义
#  两种模式共用这一份定义 —— 这是「过程透明」的关键
# ══════════════════════════════════════════════════════════════
def step_env():
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def spawn_detached(args, cwd=None):
    """启动一个**长期运行的外部服务**，并让它脱离应用进程树。

    为什么必须这样做：应用支持「停止」，停止的实现是 taskkill /T 杀整棵进程树。
    如果 Docker Desktop / Syncthing 是由应用 Popen 出来的子进程，它们会被
    一起杀掉 —— 实测踩过这个坑：点停止后 Syncthing 被误杀，后续步骤全部失败。

    实现方式：交给 PowerShell 的 Start-Process。
    直接用 subprocess 的 DETACHED_PROCESS / CREATE_BREAKAWAY_FROM_JOB 实测**无效**
    —— 父进程仍是本应用，taskkill /T 照样杀得到。
    Start-Process 由 PowerShell 自己 fork，父进程是 PowerShell 而非本应用，
    因此应用退出或被 taskkill 都不影响它（已实测验证）。
    """
    exe = args[0]
    rest = list(args[1:])
    # 构造 PowerShell 参数列表：每个参数都用单引号包住并转义内部单引号
    def q(x):
        return "'" + str(x).replace("'", "''") + "'"

    arglist = ",".join(q(a) for a in rest)
    ps = (f"Start-Process -FilePath {q(exe)}"
          + (f" -ArgumentList @({arglist})" if rest else "")
          + (f" -WorkingDirectory {q(cwd)}" if cwd else "")
          + " -WindowStyle Hidden")
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True)
        return True
    except Exception as exc:
        log(f"独立启动失败，退回直接启动: {exc}", "warn")
        try:
            subprocess.Popen(args, cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except Exception as exc2:
            log(f"启动失败: {exc2}", "error")
            return False


def syncthing_ping(gui, api_key, timeout=5):
    """探测 Syncthing GUI 是否可用。

    返回 None 表示正常，否则返回错误说明字符串
    —— 报出具体原因比只说「未响应」有用得多。
    """
    import urllib.request
    url = (gui or "http://127.0.0.1:8384").rstrip("/") + "/rest/system/status"
    hdr = {"X-API-Key": api_key} if api_key else {}
    try:
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(1)
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def proc_running(image):
    """检查某个可执行进程是否在运行（用 tasklist，比 Get-Process 更可靠）"""
    try:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        return image.lower() in (r.stdout or "").lower()
    except Exception:
        return False


def s_docker_start(cfg):
    exe = cfg["docker"]["desktop_exe"]
    log("检查 Docker Desktop…")
    if not proc_running("Docker Desktop.exe"):
        if not os.path.exists(exe):
            log(f"找不到 Docker Desktop.exe: {exe}", "error")
            return False
        log("启动 Docker Desktop（首次约 30-60 秒）…")
        spawn_detached([exe])
    ok = False
    for i in range(60):
        p = subprocess.run([DOCKER, "info"], capture_output=True)
        if p.returncode == 0:
            ok = True
            break
        time.sleep(3)
        if i % 5 == 4:
            log(f"  等待引擎就绪… ({(i + 1) * 3}s)")
    if not ok:
        log("Docker 引擎 180 秒内未就绪", "error")
        return False
    log("Docker 引擎就绪", "ok")
    return True


def s_containers_start(cfg):
    names = cfg["docker"]["containers"]
    p = subprocess.run([DOCKER, "ps", "--format", "{{.Names}}"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    running = set((p.stdout or "").split())
    todo = [n for n in names if n not in running]
    if not todo:
        log("容器已在运行", "ok")
        return True
    ok, _ = run_cmd([DOCKER, "start", *todo])
    time.sleep(8)
    return ok


def s_syncthing_start(cfg):
    s = cfg["sync"]
    if not proc_running("syncthing.exe"):
        exe = s["syncthing_exe"]
        if not os.path.exists(exe):
            log(f"找不到 syncthing.exe: {exe}", "error")
            return False
        log("启动 Syncthing…")
        # 必须独立启动：否则点「停止」时会被 taskkill /T 一起杀掉
        spawn_detached([exe, *s["syncthing_args"]], cwd=os.path.dirname(exe))
    else:
        log("Syncthing 进程已在运行")

    last_err = None
    for i in range(60):                       # 60 × 2s = 120s（首次启动可能较慢）
        err = syncthing_ping(s["syncthing_gui"], s.get("api_key", ""))
        if err is None:
            log("Syncthing GUI 就绪", "ok")
            return True
        last_err = err
        if i % 5 == 4:
            log(f"  等待 Syncthing GUI… ({(i + 1) * 2}s)")
        time.sleep(2)

    log("Syncthing GUI 120 秒内未响应", "error")
    if last_err:
        log(f"  最后一次探测错误: {last_err}", "error")
    log("  排查建议：", "error")
    log("    1) 是否已有另一个 Syncthing 实例占用配置目录（会报 lock 冲突）", "error")
    log(f"    2) 端口是否被占用：netstat -ano | findstr 8384", "error")
    log(f"    3) 手动运行看报错：\"{s['syncthing_exe']}\" "
        + " ".join(s["syncthing_args"]), "error")
    log(f"    4) 看日志：C:\\ProgramData\\Syncthing\\data\\syncthing.log", "error")
    return False


def s_download(cfg):
    d = cfg["download"]
    # 无损严格模式：只跟基线之后的新歌，不回填历史。
    # 打开时**必须彻底忽略 lossless_target** —— 不是「传个大值」而是
    # 明确告诉脚本「不限量、只取新增」。避免两个旋钮互相打架出 bug。
    strict = bool(d.get("only_new_lossless", True))
    lossless_target = 10 ** 6 if strict else int(d.get("lossless_target") or 30)

    policy = {
        "accept_hires": bool(d.get("accept_hires", True)),
        "accept_lossless": bool(d.get("accept_lossless", True)),
        "accept_lossy": bool(d.get("accept_lossy", False)),
        "min_bitrate": int(d.get("min_bitrate") or 0),
        "only_new_lossless": strict,
    }
    args = [DOCKER, "exec",
            "-e", "DOWNLOAD_POLICY=" + json.dumps(policy, ensure_ascii=False),
            "-e", f"LIBRARY_DIR={CONTAINER_LIB}",
            "music-monitor", "python", cfg["advanced"]["manage_library_py"],
            "--workers", "4",
            "--target", str(d["max_albums"]),
            "--lossless-target", str(lossless_target),
            "--sort", d["sort_mode"]]
    log(f"下载策略: Hi-Res={policy['accept_hires']} "
        f"无损={policy['accept_lossless']} 有损={policy['accept_lossy']} "
        f"最低码率={policy['min_bitrate']}kbps")
    log("无损范围: " + ("仅基线后的新歌（不限量，不回填历史）"
                        if strict else
                        f"含历史回填，目标 {lossless_target} 首"))
    log(f"曲库目录（容器内）: {CONTAINER_LIB}")
    ok, _ = run_cmd(args, step="download", env=step_env())
    return ok


def staged_hires_count():
    """读计划里 Hi-Res 的条目数 —— 用作 library-manager 的 -HiResTarget。

    为什么不能数暂存文件数：暂存里同时有 Hi-Res 和无损
    （例如 234 Hi-Res + 28 无损 = 262 个文件），
    而 -HiResTarget 比的是**计划里 Hi-Res 的数量**。
    拿 262 去比 234 会触发 library-manager 的安全中止
    （实际数 < 目标-15 就判定「下载未完成」）—— 踩过这个坑。

    所以直接读 plan.json 的 hires 数组长度，这才是它要的数字。
    """
    try:
        r = subprocess.run(
            [DOCKER, "exec", "music-monitor", "python", "-c",
             "import json;"
             "p=json.load(open('/app/data/plan.json',encoding='utf-8'));"
             "print(len(p.get('hires') or []))"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
        s = (r.stdout or "").strip()
        return int(s) if s.isdigit() else None
    except Exception:
        return None


def _verify_audio(path):
    """解码产物真的能当音频读吗？（用 ffprobe 实测，不信扩展名）

    只认「ffprobe 报得出时长」这一条 —— 加密残留、半个文件、HTML 错误页
    都会在这里露馅。读不出就返回 False，调用方据此拒绝删除原件。
    """
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)
        return bool((r.stdout or "").strip()) and float(
            (r.stdout or "0").strip() or 0) > 0
    except Exception:                       # noqa: BLE001
        return False


def _is_encrypted(p):
    """这个文件是加密容器吗？

    两条判据：
      · 扩展名就是加密容器（.ncm/.qmc*/.kgm/.kgma/.vpr/.kgg）
      · 扩展名是 .flac/.mp3，但文件头是酷狗/酷我的加密头 —— 这两家会把
        加密文件**改名成 .flac 或 .mp3**，光看扩展名认不出来

    为什么不能只看扩展名：库里已经有 72 个「伪装成 .mp3 的 FLAC」，
    扩展名骗人是这行的常态。只看扩展名的话，加密文件和普通文件会混在
    一起，要么漏掉加密的，要么把几百个正常 flac 白送给解码器去试。

    判据只比文件头的**前 12 字节**（KGM_HEADER / VPR_HEADER），不是
    「文件头不等于 fLaC」—— 那样会把 ogg、wav、以及所有坏文件都算成
    加密的。为什么是 12 而不是 16，见上面常量的注释。

    注意：判成「是加密文件」不等于「一定解得开」。酷狗换过密钥方案，
    有一批文件头是同一族、但解出来不是音频（实测解出 .bin 乱码）。
    所以解码完必须用 ffprobe 验一遍产物，见 _verify_audio()。
    """
    ext = os.path.splitext(p)[1].lower()
    if ext in DECODE_EXT:
        return True
    if ext in (".flac", ".mp3"):
        try:
            with open(p, "rb") as fh:
                head = fh.read(12)
        except OSError:
            return False
        return head.startswith(KGM_HEADER) or head.startswith(VPR_HEADER)
    return False


def _starts_kgm(p):
    """文件头是不是酷狗那一族（不管文件叫什么名字）"""
    try:
        with open(p, "rb") as fh:
            return fh.read(12).startswith(KGM_HEADER)
    except OSError:
        return False


def _decode_scan(root):
    """扫一个目录里待解码的加密音乐。返回路径列表（跳过临时残留）"""
    out = []
    for r, _dirs, files in os.walk(root):
        for f in files:
            if f.endswith(".tmp") or f.endswith(".decoding"):
                continue
            p = os.path.join(r, f)
            if _is_encrypted(p):
                out.append(p)
    return sorted(out)


def _decode_via_kgg(exe, src, outdir, threads=0, probe_dir=None):
    """酷狗最后手段：**复制成 .kgg 再解**。成功返回产物路径，失败返回 None。

    为什么需要这一招（踩过的坑）
    ----------------------------
    music-decode 按**扩展名**决定走哪条路：

      · `.kgg`          → 交给 kgg-helper.exe 子进程（它认得酷狗全系容器）
      · `.kgm/.kgma/...` → 走 KGM 路径，但那条路要求文件头前 16 字节
                          精确等于源码里写死的常量

    而实测磁盘上的文件普遍是 `...0f ff 99 14`，源码常量是 `...0f f9 91 e4`
    —— 对不上，于是整族文件被静默判成「不支持」。用户导入 `.kgma` 失败
    就是这么来的。

    既然 kgg-helper 那条路更宽容，就干脆**不管原来叫什么名字，复制一份
    `.kgg` 再解**。实测 `.kgma`（内容其实是 KGG 容器）这样能正常还原出
    flac；而普通 flac 冒充 `.kgg` 会被干净地判失败，不会产出垃圾。
    多复制一份文件的代价只在酷狗这一族上发生，可以接受。

    注意：解出来可能不是音频（`.bin`），所以调用方**必须**再用
    _verify_audio() 验一遍，本函数不做这个判断。
    """
    fam = os.path.splitext(src)[1].lower() in KGM_FAMILY_EXT or _starts_kgm(src)
    if not fam:
        return None

    probe = os.path.join(probe_dir or BASE, "_probe.kgg")
    os.makedirs(os.path.dirname(probe), exist_ok=True)
    try:
        shutil.copy2(src, probe)
        args = [exe, "-o", outdir]
        if int(threads or 0) > 0:
            args += ["-t", str(int(threads))]
        args.append(probe)
        subprocess.run(args, capture_output=True, timeout=1800)
    except Exception:                       # noqa: BLE001
        return None
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass

    if not os.path.isdir(outdir):
        return None
    for f in sorted(os.listdir(outdir)):
        p = os.path.join(outdir, f)
        if os.path.isfile(p) and _verify_audio(p):
            return p
    return None


def _decode_via_python(src, outdir):
    """用纯 Python 的酷狗解密模块解一次。成功返回产物路径，失败返回 None。

    为什么把它排在 external 解码器**前面**（顺序不能反）
    ---------------------------------------------------
    music-decode 的 KGM 路径认的是写死的 16 字节头，对真实文件一律判「不支持」；
    而它的 `.kgg` 路径（kgg-helper）对这批文件会**报成功但产出乱码**。也就是说
    外置解码器在酷狗这一族上只有「静默跳过」和「静默出错」两种结局。
    先试本模块，成功就省掉一次 1800 秒超时级别的外部调用，失败再退回去。

    只吃 .kgma/.kgm/.kgtemp（密钥随文件走的那几类）。**不吃 .kgg**：
    kgg 的密钥不在文件里（在酷狗客户端本地缓存），本模块喂进去只会出乱码，
    can_decrypt() 会先挡掉，.kgg 仍然交给 kgg-helper。

    产物仍然要过 _verify_audio()（调用方做），因为「解码器说成功」不等于
    「解对了」—— 这个坑在酷狗这一族上是踩过的。
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
    os.makedirs(outdir, exist_ok=True)
    try:
        return mod.decrypt_file(src, outdir)
    except Exception as exc:                   # noqa: BLE001
        log(f"      内置酷狗解密失败（改走外置解码器）: {exc}", "warn")
        return None


def s_decode(cfg):
    """解码加密音乐 → 普通 flac/mp3/ogg

    为什么必须有这一步
    ------------------
    网易/QQ/酷狗客户端下载的歌是**加密容器**（.ncm/.qmc*/​.kgm/.vpr/.kgg）。
    加密文件用 ffprobe 读不出格式与时长，所以：

      · 采样率闸门会把它当坏文件（读不出规格 = 可疑）
      · 落盘去重要按规格择优，也拿不到规格
      · 手机上的播放器根本不认这些格式

    也就是说，加密文件在这条流水线里**走不到最后**，会在闸门那一步被丢掉。

    顺序为什么在这里
    ----------------
    「④ 下载」把文件放进容器暂存，library-manager 在「⑤」里把暂存镜像到
    本机（robocopy /MIR）再跑闸门。所以要赶在**闸门之前、镜像之后**解码，
    否则这一轮的文件已经因为读不出规格被判掉了。

    解码是**无损还原**，不是转码：ncm 里包的是 flac 就出 flac，包的是 mp3
    就出 mp3，字节完全一样，所以不会破坏音质。

    安全设计（不能乱来）
    --------------------
    · 先解码到临时目录，用 ffprobe 验过「读得出时长」才替换原件；
    · 校验不过就保留加密原件并报错，绝不删掉唯一的一份；
    · 替换走 os.replace 原子操作，避免同步到半个文件。
    """
    dc = cfg.get("decode") or {}
    exe = cfg["advanced"].get("music_decode_exe") or MUSIC_DECODE

    if not dc.get("enabled", True):
        log("解码加密音乐已在设置里关闭，跳过", "warn")
        return True
    if not os.path.exists(exe):
        log(f"找不到解码器 {exe}，跳过（加密文件这一轮仍会被闸门丢掉）", "warn")
        return True

    # 两个暂存目录都要解，而且**必须都解**：
    #   hires_staging —— 容器直连的宿主目录（容器 /app/data/hires_staging）
    #   _staging      —— library-manager 用 robocopy /MIR 镜像出来的本机副本
    # library-manager 的闸门跑在 _staging 上，而 /MIR 会用容器那份覆盖它，
    # 只解一边的话，另一边下一轮就把解密结果盖回去了。
    targets = []
    for root in (HIRES_STAGING, LOCAL_STAGING):
        if os.path.isdir(root):
            found = _decode_scan(root)
            if found:
                log(f"  {root} 里有 {len(found)} 个加密文件")
            targets += found
    if not targets:
        log("没有加密格式的音乐需要解码（都是普通 flac/mp3）")
        return True

    log(f"发现 {len(targets)} 个加密音乐，开始解码（无损还原，不是转码）")
    tmpdir = os.path.join(BASE, "_decode_tmp")
    ok_n = fail_n = 0
    fails = []

    probe_dir = os.path.join(BASE, "_probe_tmp")
    shutil.rmtree(probe_dir, ignore_errors=True)

    for i, src in enumerate(targets, 1):
        name = os.path.basename(src)
        # 输出到临时目录，解码器会按**还原出来的真实格式**决定扩展名。
        # 每首清一次，避免上一首的残留被当成本次产物。
        shutil.rmtree(tmpdir, ignore_errors=True)
        os.makedirs(tmpdir, exist_ok=True)

        def _attempt(path, extra_note=""):
            """解一次，返回能当音频读的产物路径；没有就返回 None。"""
            args = [exe, "-o", tmpdir]
            if int(dc.get("threads") or 0) > 0:
                args += ["-t", str(int(dc["threads"]))]
            args.append(path)
            # 退出码**故意不参与判断**。music-decode 在收尾的 TagLib 写标签
            # 阶段会踩内存崩掉，进程以 0xC0000005 退出（实测退出码
            # 3221225477 / -1073741819 / -1073740940），但音频**在那之前
            # 就已经完整写完了**。拿退出码当成败的话，每一个加密文件都会
            # 被判失败、原件也不会删。所以只看产物能不能当音频读。
            run_cmd(args, step="decode", env=step_env())
            produced = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)] \
                if os.path.isdir(tmpdir) else []
            # 只认能当音频读的产物 —— 这一条挡的是「解码器报了成功，
            # 但出来的东西其实不是音频」（残缺密钥、半个文件都会这样）。
            for p in produced:
                if _verify_audio(p):
                    if extra_note:
                        log(f"      {extra_note}")
                    return p
            return None

        # 顺序（为什么先内置、后外置）
        # ------------------------------
        # ① 内置 Python 模块：唯一能真解 .kgma/.kgm/.kgtemp 的路（密钥随文件走）
        # ② 外置解码器：管 .ncm/.qmc*/.kgg 这些内置模块不管的容器
        # ③ 复制成 .kgg 再交给 kgg-helper：碰「内容其实是 KGG 容器、但名字叫
        #    .kgma」这种情况
        # 先跑内置的另一个原因：外置解码器在酷狗这一族上会「报成功但产出乱码」，
        # 让它先跑反而会把乱码当成功（_verify_audio 能挡住，但白等一次超时）。
        good = _decode_via_python(src, tmpdir)
        if good is not None and not _verify_audio(good):
            # 解出来读不出音频 = 密钥不匹配 → 不当成功，交给后面的路重试
            log("      内置酷狗解密产物读不出音频，继续尝试其他方式", "warn")
            good = None
        if good is not None:
            log("      内置酷狗解密成功（.kgma/.kgm/.kgtemp 走这条路）")

        if good is None:
            good = _attempt(src)

        # 还没成，且这是酷狗那一族的，就换名字再试一遍。
        # 原因见 _decode_via_kgg() 的注释：解码器按扩展名分流，而酷狗换了
        # 一次密钥方案之后，KGM 那条路认不出新文件头了。
        if good is None and (os.path.splitext(src)[1].lower() in KGM_FAMILY_EXT
                             or _starts_kgm(src)):
            alt = _decode_via_kgg(exe, src, tmpdir, dc.get("threads"), probe_dir)
            if alt:
                good = alt
                log("      按 .kgg 路径重试成功（酷狗换过密钥方案，"
                    "按原名会被判成不支持）")

        if good is None:
            fail_n += 1
            fails.append(name)
            log(f"  [{i}/{len(targets)}] ✗ 解码失败，保留加密原件: {name}", "error")
            continue

        out = good
        dst = os.path.join(os.path.dirname(src), os.path.basename(out))
        try:
            tmp = dst + ".decoding"
            shutil.copy2(out, tmp)          # 先落 .decoding，别人读不到半个文件
            os.replace(tmp, dst)            # 再原子换上去
        except Exception as exc:            # noqa: BLE001
            fail_n += 1
            fails.append(name)
            log(f"  [{i}/{len(targets)}] ✗ 写入失败: {exc}", "error")
            continue

        ok_n += 1
        src_size = os.path.getsize(src)
        log(f"  [{i}/{len(targets)}] ✓ {name} → {os.path.basename(dst)} "
            f"({src_size / 1048576:.1f} MB)")

        # 校验都过了才删加密原件。
        # 删除失败不算错 —— 文件已经解出来了，留着原件只是多点地方。
        if dc.get("delete_source", True):
            try:
                os.remove(src)
            except OSError as exc:
                log(f"      原加密文件删除失败（不影响已解码的文件）: {exc}", "warn")

    shutil.rmtree(probe_dir, ignore_errors=True)
    shutil.rmtree(tmpdir, ignore_errors=True)   # 收尾：不让中间产物留在同步范围里

    log(f"解码完成：成功 {ok_n}，失败 {fail_n}")
    if fails:
        log("以下文件解码失败，加密原件已保留（可手动处理）: " + "、".join(fails[:5])
            + ("…" if len(fails) > 5 else ""), "warn")
    return True


def s_maintain(cfg):
    """取暂存 → 采样率闸门 → 落盘 → 去重 → 写标签（走既有的 library-manager）"""
    d = cfg["download"]
    target = int(d["max_albums"])

    actual = staged_hires_count()
    if actual:
        # 计划里的 Hi-Res 数量才是权威值。
        # library-manager 的判定是「实际 < 目标-15 就中止」，所以：
        #   目标比实际大 → 必然误报中止，必须下调
        #   目标比实际小一点（<15）→ 无害，保留用户设置
        #   目标比实际小很多   → 可能把想保留的曲目精简掉，上调到实际
        if target > actual:
            log(f"计划里 Hi-Res 实际 {actual} 首，把目标从 {target} 下调为 {actual}"
                f"（避免误判「下载未完成」而中止）", "warn")
            target = actual
        elif target < actual - 15:
            log(f"目标 {target} 明显小于实际 {actual}，上调为 {actual}", "warn")
            target = actual

    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", cfg["advanced"]["library_manager"],
            "-Apply", "-SkipDownload",
            "-HiResTarget", str(target),
            "-LosslessTarget", str(d["lossless_target"]),
            "-SortMode", d["sort_mode"]]
    if not cfg["guard"].get("block_sample_rates"):
        args.append("-SkipRateGate")
    ok, _ = run_cmd(args, step="maintain", env=step_env())
    return ok


def s_covers(cfg):
    """补齐封面与歌词。

    为什么单独有这一步
    ------------------
    go-music-dl 引擎有两条下载路径（源码 internal/web/music.go）：

      GET  /music/download?embed=1               -> DownloadSongData，**会**嵌封面
      POST /music/download?embed=1&save_local=1  -> DownloadWithDedup，落盘但**不**嵌封面

    music-monitor 需要 JSON 结果来判断 skipped/filename，所以只能走 POST，
    于是封面永远嵌不进去；而网易的解析器压根没有 lyrics 字段，歌词也从没传过。
    实测曲库 268 首里带封面的只有 7 首，且全是用户自己导入的原盘文件。

    所以在本机补：歌名歌手从文件名 "歌手 - 歌名" 解析，先查网易云（歌手要对得上），
    查不到再退 iTunes；封面写 IMAGE_BLOCK，歌词写 LYRICS 标签。

    每轮都全库扫一遍检查缺什么 —— 实测 268 首读元数据只要 0.12 秒，
    所以已有封面/歌词的歌曲不会有网络开销，可以放心每轮跑。

    「查过了没有」的状态由脚本自己存到 covers_state.json（默认路径，
    这里不用传参）。**不要再把状态写回音频文件** —— 曲库是 Syncthing 的
    sendonly 文件夹，改一个字节就要把整首歌重传给手机（2026-10-15 修）。
    """
    script = cfg["advanced"].get("fix_covers_py")
    if not script or not os.path.exists(script):
        log("找不到封面脚本，跳过", "warn")
        return True

    c = cfg.get("covers") or {}
    args = [PY, script, "--dir", cfg["sync"]["downloads_dir"], "--sleep", "0.3"]
    if c.get("enabled", True):
        args.append("--apply")
        args += ["--max-per-run", str(int(c.get("max_per_run", 0)))]
        if c.get("lyrics", True):
            args.append("--lyrics")
        # 歌词也查网易云：LRCLIB 的中文库偏薄（中文歌经常整个查不到），
        # 而网易云中文全。代价是网易云会限流，所以靠 fix_covers 自己的
        # 冷却 + 连续 8 首 BUSY 就收工来兜住；而且**查过的不会重复查**
        # （负结果写进文件标签，30 天才重试一次），所以只有新歌才花这份钱。
        if c.get("netease_lyrics", True):
            args.append("--netease-lyrics")
        args.append("--quiet")
    else:
        log("封面补全已在设置里关闭，只统计不修改", "warn")

    ok, _ = run_cmd(args, step="covers", env=step_env())
    return ok


def s_verify(cfg):
    guard = cfg["advanced"]["samplerate_guard_py"]
    if not os.path.exists(guard):
        log("找不到闸门脚本，跳过", "warn")
        return True
    g = cfg["guard"]
    policy = {
        "block_sample_rates": g.get("block_sample_rates") or [],
        "block_channels": g.get("block_channels") or [],
    }
    log(f"拦截策略: 采样率={policy['block_sample_rates'] or '不拦'} "
        f"声道={policy['block_channels'] or '不拦'}")
    env = step_env()
    env["GUARD_POLICY"] = json.dumps(policy, ensure_ascii=False)
    ok, _ = run_cmd([PY, guard, "--apply"], step="verify", env=env)
    return ok


def s_integrity(cfg):
    chk = cfg["advanced"]["check_integrity_py"]
    if not os.path.exists(chk):
        log("找不到完整性检查脚本，跳过", "warn")
        return True
    ok, _ = run_cmd([PY, chk], step="integrity", env=step_env())

    # 2026-10-18 加：顺带查「同一歌手撞名」和「同一首音频存了两份」。
    # 只报告、不修改 —— 删文件或改标签都不该在体检步骤里静默发生。
    # 要修复请手动跑：
    #   python scripts/library_guard.py --fix-tags
    #   python scripts/library_guard.py --fix-dup
    guard = os.path.join(BASE, "scripts", "library_guard.py")
    if os.path.exists(guard):
        gok, _ = run_cmd([PY, guard], step="integrity", env=step_env())
        if not gok:
            log("曲库守护（撞名/重复副本）检查没跑通，不影响本轮其它步骤", "warn")
    return ok


def sync_pending_report(cfg, since_seconds=3600):
    """在盯进度之前，先告诉用户「这次到底要传什么、为什么」。

    背景（2026-10-14 加）
    --------------------
    用户看到的现象是「怎么又重头下载」。根因是流水线里有步骤会**无条件
    改写文件**（哪怕内容没变），Syncthing 就把它当成新版本、整个重传。
    所以同步步骤必须自报家门：这次有多少文件是刚刚被动过的、一共多大。
    这样「重传」要么有据可查，要么就是真有问题，不用再去翻 Syncthing 日志。
    """
    d = cfg["sync"]["downloads_dir"]
    if not os.path.isdir(d):
        return
    audio_exts = (".flac", ".mp3", ".m4a", ".ogg", ".wav")
    now = time.time()
    recent = []
    total = 0
    try:
        for fn in os.listdir(d):
            if os.path.splitext(fn)[1].lower() not in audio_exts:
                continue
            p = os.path.join(d, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if now - st.st_mtime <= since_seconds:
                recent.append((st.st_mtime, fn, st.st_size))
                total += st.st_size
    except OSError as exc:
        log(f"统计近期改动失败: {exc}", "warn")
        return
    if not recent:
        log("近 1 小时没有文件被改写 —— 这次同步应当不需要重传任何内容", "ok")
        return
    recent.sort(reverse=True)
    log(f"近 1 小时有 {len(recent)} 个文件被改写，共 {total / 1048576:.1f} MB"
        f"（这些会被同步重传）", "warn")
    for m, fn, sz in recent[:8]:
        log(f"    {time.strftime('%H:%M:%S', time.localtime(m))}  "
            f"{sz / 1048576:6.1f} MB  {fn[:56]}")
    if len(recent) > 8:
        log(f"    … 另 {len(recent) - 8} 个")


# ══════════════════════════════════════════════════════════════
# 把曲库里的改动回写到 _staging
# ══════════════════════════════════════════════════════════════
# 2026-10-15 新增。为什么必须要有这一步（实测数据说话）
# ----------------------------------------------------------
# ⑥落盘是单向的：_staging -> 曲库。可是 ⑤（tag_files.py 按文件名规范化标签）
# 和 ⑦（补封面/歌词）改的**只有曲库**，_staging 那边永远是原始版本。
# 于是同一首歌两边永久不同：
#
#     第 N   轮  ⑥ 把暂存拷进库（这时库里没封面）
#     第 N   轮  ⑦ 给库补封面、⑤ 规范标签     -> 库 != 暂存
#     第 N+1 轮  ⑥ 比对发现「内容不同」-> 整份覆盖
#                -> 上轮的封面/歌词被抹掉，落盘 148 个、重传 7 GB
#     第 N+1 轮  ⑦ 发现库里又没封面了 -> 重新联网下载再嵌一遍
#
# 实测：一次 ⑥ 抹掉 84 个封面、82 份歌词，152 个文件（7.07 GB）被判为
# 「内容不同」；Syncthing 自己的账也是 156 项 / 7222.8 MB ——完全对得上。
#
# 修法就是补上这条缺失的边：⑦之后把曲库**晚于**暂存的那一份回写过去。
# 只回写「内容真的不同」的文件（大小 -> SHA256），相同的不碰；
# 只处理暂存里已经存在的文件（不往暂存里塞新歌，暂存仍是下载源的样子）。
AUDIO_EXT = (".flac", ".mp3", ".m4a", ".ogg", ".wav", ".wma", ".aac", ".ape")


def _same_file(a, b, chunk=1 << 20):
    """两个文件内容是否完全相同（大小 -> 分块 SHA256）。读不了就返回 False。"""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        ha = hashlib.sha256()
        hb = hashlib.sha256()
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ba = fa.read(chunk)
                bb = fb.read(chunk)
                if ba != bb:
                    return False
                if not ba:
                    return True
    except OSError:
        return False


def _copy_library_to(src, dst):
    """把内容已经变了的文件写回暂存，**并把 mtime 一起带过去**。

    为什么连 mtime 都要带（踩过的坑）
    --------------------------------
    Windows 的 Copy-Item / shutil.copy2 有「目标较新就不覆盖」的行为，
    所以必须显式 os.utime。更要紧的是：⑥那边判定用的就是内容，
    这里把 mtime 对齐之后，两边的 stat 也一致，排查问题时不会再被
    「明明内容一样却显示时间不同」误导。
    """
    tmp = dst + ".coverback"
    shutil.copyfile(src, tmp)
    shutil.copystat(src, tmp)
    os.replace(tmp, dst)
    try:
        st = os.stat(src)
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass


def s_coverback(cfg):
    """把曲库里⑤/⑦改过的文件回写到暂存目录。

    为什么要回写**两个**暂存目录（2026-10-15 实测踩到的坑）
    ----------------------------------------------------
    `_staging` 不是源头，它只是 library-manager 阶段 2 用
    `robocopy $stagingDirect $staging /MIR` 从 `hires_staging` 镜像出来的副本。
    只回写 `_staging` 的话，下一轮⑥一开始就会：
        阶段 2  /MIR 用 hires_staging 覆盖 _staging   -> 回写成果全没了
        阶段 3  /MIR 内容比对又发现「不同」            -> 又整份覆盖 109~137 个
        阶段 5  刚补好的标签又被覆盖回原始版本         -> 又「需补写 71 个」
    实测：只回写 _staging 时，下一轮⑥照样落盘 109 个、⑤照样重写 71 个。
    所以必须**沿着数据流往上游回写**，回到镜像的源头 hires_staging。

    不做的事（刻意为之）：
      * 不新增任何数据库 / 队列 / 同步历史 —— 就是一次目录比对
      * 不往暂存里加曲子（暂存里没有的文件一律跳过，只记录数量）
      * 不碰相同文件（相同 = 不写盘 = 不刷新 mtime = 不触发同步重传）
    """
    lib = cfg["sync"]["downloads_dir"]
    if not os.path.isdir(lib):
        log(f"曲库目录不存在 {lib}，跳过", "warn")
        return True
    targets = [cfg["sync"].get("staging_dir") or LOCAL_STAGING,
               HIRES_STAGING]
    seen = set()
    failed_total = 0
    for stg in targets:
        if stg in seen:
            continue
        seen.add(stg)
        failed_total += _coverback_one(cfg, lib, stg)
    return failed_total == 0


def _coverback_one(cfg, lib, stg):
    """回写一个暂存目录；返回失败个数。"""
    if not os.path.isdir(stg):
        log(f"暂存目录不存在 {stg}，跳过", "warn")
        return 0

    log(f"曲库　{lib}")
    log(f"暂存　{stg}")
    same = diff = copied = missing = failed = 0
    bytes_copied = 0
    names = []
    try:
        entries = sorted(os.listdir(stg))
    except OSError as exc:
        log(f"读暂存目录失败: {exc}", "warn")
        return 1

    for fn in entries:
        if os.path.splitext(fn)[1].lower() not in AUDIO_EXT:
            continue
        a = os.path.join(lib, fn)          # 曲库（新）
        b = os.path.join(stg, fn)          # 暂存（旧）
        if not os.path.isfile(a):
            missing += 1                   # 闸门没选中它，停在旧版本，别动
            continue
        try:
            if os.path.getsize(a) == os.path.getsize(b) and _same_file(a, b):
                same += 1
                continue
        except OSError as exc:
            log(f"  比对失败 {fn}: {exc}", "warn")
            failed += 1
            continue
        diff += 1
        names.append(fn)
        try:
            sz = os.path.getsize(a)
            _copy_library_to(a, b)
            copied += 1
            bytes_copied += sz
        except OSError as exc:
            failed += 1
            log(f"  回写失败 {fn}: {exc}", "warn")

    log(f"内容相同: {same} 个（未碰暂存）")
    log(f"内容不同: {diff} 个（曲库晚于暂存，已被⑤/⑦改过）")
    if missing:
        log(f"暂存里没有: {missing} 个（闸门未选中的老文件，保持原样）")
    for fn in names[:8]:
        log(f"    {fn[:60]}")
    if len(names) > 8:
        log(f"    … 另 {len(names) - 8} 个")
    if copied:
        log(f"已回写 {copied} 个文件到暂存，共 {bytes_copied / 1048576:.1f} MB", "ok")
        log("下一轮⑥落盘对这些文件会判定「内容未变」而跳过 —— "
            "⑥⑦之间不再来回覆盖，Syncthing 也就不会再看到假变化", "ok")
    else:
        log("暂存与曲库已经一致，无需回写", "ok")
    if failed:
        log(f"有 {failed} 个文件回写失败，下一轮⑥会重新覆盖它们", "warn")
    return failed


def s_sync(cfg):
    """触发扫描并盯同步进度。

    设计要点：
    1. 先确认 Syncthing 在跑 —— 没跑就自动拉起（单跑本步时很方便）
    2. 花很短时间判断手机是否可达；连不上就立刻明确报错并结束
       —— 而不是无脑等 20 分钟（之前就是这个行为，看起来像「卡死」）
    """
    import urllib.request
    s = cfg["sync"]
    hdr = {"X-API-Key": s["api_key"]}

    def api(path, method="GET", timeout=20):
        req = urllib.request.Request(s["syncthing_gui"] + path,
                                     headers=hdr, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return json.loads(body) if body else {}

    def gui_alive():
        return syncthing_ping(s["syncthing_gui"], s.get("api_key", "")) is None

    # ---- 0. 确保 Syncthing 在运行 ----
    if not s.get("api_key") or not s.get("phone_device"):
        log("Syncthing 配置不完整 —— 请在「设置」里填写 API Key 和手机设备 ID", "error")
        log("  获取方式：Syncthing GUI → 操作 → 设置 → API 密钥", "error")
        log("            Syncthing GUI → 设备列表点开手机 → 设备 ID", "error")
        return False

    if not gui_alive():
        log("Syncthing 未运行，尝试启动…", "warn")
        exe = s["syncthing_exe"]
        if os.path.exists(exe):
            # 独立启动，避免被「停止」误杀
            spawn_detached([exe, *s["syncthing_args"]],
                           cwd=os.path.dirname(exe))
        else:
            log(f"找不到 syncthing.exe: {exe}", "error")
            return False
        last_err = None
        for i in range(45):                   # 45 × 2s = 90s
            err = syncthing_ping(s["syncthing_gui"], s.get("api_key", ""))
            if err is None:
                log("Syncthing 已启动并就绪", "ok")
                break
            last_err = err
            time.sleep(2)
        else:
            log(f"Syncthing 启动后 90 秒内 GUI 仍不可达（{last_err}）", "error")
            return False

    # ---- 1. 触发本地扫描 ----
    try:
        api(f"/rest/db/scan?folder={s['folder_id']}", "POST", timeout=60)
        log("已触发本地扫描")
    except Exception as exc:
        log(f"触发扫描失败: {exc}", "error")
        return False

    # ---- 1.5 说清楚这次要传什么（避免「怎么又重头下载」的困惑）----
    sync_pending_report(cfg)

    # ---- 2. 快速判断手机可达性（最多 90 秒）----
    log("检查手机是否在线…")
    last_err = None
    connected = False
    for i in range(30):                       # 30 × 3s = 90s
        try:
            conn = api("/rest/system/connections")
            pc = conn["connections"].get(s["phone_device"], {})
            if pc.get("connected"):
                connected = True
                log(f"手机已连接（{pc.get('address') or '未知地址'}）", "ok")
                break
            if pc.get("paused"):
                log("手机端 Syncthing 处于「已暂停」状态", "warn")
        except Exception as exc:
            last_err = exc
        if i % 5 == 4:
            log(f"  等待手机连接…（{(i + 1) * 3}s）")
        if _STOP_EVENT.is_set():
            log("已被用户停止（还没开始传，手机端不受影响）", "warn")
            raise StepAborted()
        time.sleep(3)

    if not connected:
        log("手机未连接 —— 同步无法进行。请检查：", "error")
        log("  1) 手机上的 Syncthing 是否在运行（Android 省电策略常把它杀掉）", "error")
        log("  2) 手机与电脑是否在同一网络（热点/局域网）", "error")
        log("  3) 手机端是否把电脑设备设为「已暂停」", "error")
        if last_err:
            log(f"  最近一次查询错误: {last_err}", "warn")
        try:
            comp = api(f"/rest/db/completion?device={s['phone_device']}"
                       f"&folder={s['folder_id']}")
            log(f"当前进度 {round(comp['completion'], 2)}%，"
                f"还有 {comp['needItems']} 项 / "
                f"{comp['needBytes'] / 1048576:.1f} MB 待传（等手机上线后会自动继续）",
                "warn")
        except Exception:
            pass
        return False

    # ---- 3. 盯进度 ----
    last = -1
    stall = 0
    for i in range(240):                      # 最多再盯 20 分钟
        try:
            api("/rest/system/connections")
            comp = api(f"/rest/db/completion?device={s['phone_device']}"
                       f"&folder={s['folder_id']}")
            pct = round(comp["completion"], 2)
            if pct != last:
                log(f"同步进度 {pct}%   剩余 "
                    f"{comp['needBytes'] / 1048576:.1f} MB / {comp['needItems']} 项",
                    "ok")
                last = pct
                stall = 0
            else:
                stall += 1
            if pct >= 100 or comp["needItems"] == 0:
                log("同步完成，手机已是最新", "ok")
                return True
            # 进度 5 分钟不动，且没有任何待传项 → 认为已完成
            if stall > 60 and comp["needItems"] == 0:
                log("无待传项，视为已完成", "ok")
                return True
        except Exception as exc:
            log(f"查询进度失败: {exc}", "warn")
        # 盯进度这一段时间里没有子进程可杀（只是在轮询 Syncthing 的 REST），
        # 所以「停止」按钮对这一步原本完全无效，最多要空等 20 分钟才结束。
        # 每轮睡眠前看一眼停止信号，让它最多 5 秒内响应。
        if _STOP_EVENT.is_set():
            log("已被用户停止（手机端的传输不受影响，会继续传完）", "warn")
            raise StepAborted()
        time.sleep(5)

    log("等待超时（进度长时间未推进），同步可能未完成", "warn")
    return False


# 步骤表：两种模式共用的唯一真相
#   soft=True 表示「这一步失败不算致命」：
#   自动模式不会因此中止后续步骤，只是把该步标为待处理。
#   典型例子是同步 —— 手机没连上时前面所有工作其实都做完了，
#   不该让整次运行显示成失败。
# 顺序里容易踩的两个坑（都踩过）：
#   · 「⑤·回写暂存」必须在「⑥ 落盘与整理」**之前** —— ⑥ 要读它写过的暂存目录，顺
#     序反了⑥读到的还是旧内容，照样当成「内容变了」重新落盘一遍。
#   · 「⑦ 封面与歌词」改完曲库后必须再回写一次，所以 coverback 只能排在⑦后面 ——
#     它排在⑦后面才是这一轮的最后一次写暂存，之后 ⑧⑨⑩ 都不再碰曲库，曲库才静得下来。
#
# critical / stop_warning 是给「停止」按钮用的：
#   这些步骤被中断会留下半成品或不可逆的中间态，前端按下停止时要先把代价说清楚。
PIPELINE = [
    {"id": "docker",     "name": "① 启动 Docker",      "desc": "拉起 Docker Desktop 并等引擎就绪", "fn": s_docker_start},
    {"id": "containers", "name": "② 启动容器",          "desc": "启动 go-music-dl / music-monitor",  "fn": s_containers_start},
    {"id": "syncthing",  "name": "③ 启动 Syncthing",    "desc": "拉起同步服务并等 GUI 就绪",          "fn": s_syncthing_start},
    {"id": "download",   "name": "④ 检查并下载新收藏",  "desc": "按设置里的音质规则抓取缺失曲目",     "fn": s_download,
     "critical": True,
     "stop_warning": "正在下载歌曲。现在停止会中断当前下载，这首会留成未完成的残缺文件，"
                     "下一轮重新下载时才会被覆盖掉。已经下载完的歌不受影响。"},
    {"id": "decode",     "name": "⑤ 解码加密音乐",      "desc": "ncm/qmc/kgm 还原成普通 flac/mp3",   "fn": s_decode,
     "critical": True,
     "stop_warning": "正在解密加密歌曲。已经解完的歌已经安全落盘；现在停止会丢掉正在解的这一首的进度，"
                     "原加密文件会原样保留，下一轮重新解一遍就行，不会损坏任何歌。"},
    {"id": "maintain",   "name": "⑥ 落盘与整理",        "desc": "取暂存→闸门→落盘→去重→写标签",      "fn": s_maintain,
     "critical": True,
     "stop_warning": "正在落盘。这一步每首歌都是「先写临时文件、再原子替换」，所以不会写出半个文件。"
                     "但落盘没有进度记忆：现在停止，这一轮已经写进去的歌会留在曲库里（内容完整），"
                     "没轮到的下一轮从头再来一遍（已落盘且内容相同的会被跳过，不会重复写）。"},
    {"id": "covers",     "name": "⑦ 封面与歌词",        "desc": "给缺封面的补齐专辑图，顺带补歌词",   "fn": s_covers,
     "critical": True,
     "stop_warning": "正在补封面/歌词。每个文件同样是「写临时文件再原子替换」，不会写坏。"
                     "现在停止，本轮已经补好的歌保留，剩下的下一轮继续补。"
                     "注意：封面限流（429）时这一步本来就慢，中途停掉这部分工作就白做了。"},
    {"id": "coverback",  "name": "⑧ 回写暂存",          "desc": "把⑥⑦改过的文件同步回暂存，"
                                                                "让⑥下次判定「内容未变」而跳过",
     "fn": s_coverback,
     "critical": True,
     "stop_warning": "正在回写暂存。现在停止会让一部分文件只回写了一半 —— "
                     "下一轮⑥读到的暂存内容与曲库不一致，那几个文件会被重新落盘一次"
                     "（不影响歌曲本身，只是多传一遍）。建议让它跑完，通常只要几秒。"},
    {"id": "verify",     "name": "⑨ 安全检查",          "desc": "拦截会炸耳的采样率，清理暂存",       "fn": s_verify},
    {"id": "integrity",  "name": "⑩ 曲库体检",          "desc": "检查文件是否损坏、标签是否完整",     "fn": s_integrity},
    {"id": "sync",       "name": "⑪ 同步到手机",        "desc": "触发扫描并盯着传到完成",             "fn": s_sync,
     "soft": True, "critical": True,
     "stop_warning": "手机端的 Syncthing 是独立在传的：停止只会结束电脑这边的「看着进度」，"
                     "手机该收的还是会继续收完。停止后本次运行记录会显示为已停止，"
                     "想继续看进度可以重新点一次同步（已经传过去的部分不会重传）。"},
]


# ══════════════════════════════════════════════════════════════
#  运行器
# ══════════════════════════════════════════════════════════════
class Runner:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.mode = None
        self.current = None
        self.results = {}          # step_id -> ok/warn/fail/skip/stopped
        self.timings = {}          # step_id -> 这一步实际跑了多少秒

    def snapshot(self):
        # current_critical / current_warning 给前端的「停止」按钮用：
        # 正在跑的这一步如果中断会有代价，按停止时先把代价弹出来问一次。
        cur = None
        if self.current:
            cur = next((s for s in PIPELINE if s["id"] == self.current), None)
        return {
            "running": self.running,
            "mode": self.mode,
            "current": self.current,
            "current_name": cur["name"] if cur else None,
            "current_critical": bool(cur and cur.get("critical")),
            "current_warning": (cur or {}).get("stop_warning"),
            "results": dict(self.results),
            "timings": dict(self.timings),
        }

    def run(self, mode, only=None):
        """mode: 'auto' 全跑 | 'manual' 只跑 only 指定的那一步"""
        with self.lock:
            if self.running:
                log("已有任务在跑，忽略本次请求", "warn")
                return
            self.running = True
            _STOP_EVENT.clear()
            self.mode = mode
            if mode == "auto":
                self.results = {}
                self.timings = {}

        cfg = load_config()
        title = "省心模式（全流程）" if mode == "auto" else f"单步：{only}"
        RECORDER.start(title)
        log(f"===== {title} =====", "head")

        steps = PIPELINE if mode == "auto" else \
            [s for s in PIPELINE if s["id"] == only]
        if not steps:
            log(f"未知步骤: {only}", "error")
            self._finish(False)
            return

        all_ok = True
        soft_fail = []
        stopped = False
        try:
            for st in steps:
                if _STOP_EVENT.is_set():
                    log("已被用户停止", "warn")
                    self.results[st["id"]] = "skip"
                    all_ok = False
                    stopped = True
                    break
                self.current = st["id"]
                BUS.publish("step_start", step=st["id"], name=st["name"])
                log(f"───── {st['name']}：{st['desc']} ─────", "head", st["id"])
                step_aborted = False
                t_step = time.monotonic()
                try:
                    ok = st["fn"](cfg)
                except StepAborted:
                    # 步骤自己在中途看到停止请求，干净地收了尾。不算失败，
                    # 也不算「这一轮干完了」—— 记成 stopped，后面不再继续跑。
                    log(f"⏹ {st['name']} 已在中途停止（这一步的工作没做完）", "warn",
                        st["id"])
                    self.results[st["id"]] = "stopped"
                    BUS.publish("step_end", step=st["id"], ok=False, stopped=True)
                    all_ok = False
                    step_aborted = True
                    stopped = True
                except Exception:
                    log("步骤抛出异常：\n" + traceback.format_exc(), "error",
                        st["id"])
                    ok = False

                if step_aborted:
                    self.timings[st["id"]] = time.monotonic() - t_step
                    break

                if ok:
                    self.results[st["id"]] = "ok"
                    BUS.publish("step_end", step=st["id"], ok=True)
                    el = self._took(st, t_step)
                    log(f"✓ {st['name']} 完成（用时 {el}）", "ok", st["id"])
                    continue

                # 失败：软步骤（如手机没连上导致的同步失败）不算致命
                if st.get("soft"):
                    self.results[st["id"]] = "warn"
                    BUS.publish("step_end", step=st["id"], ok=False, soft=True)
                    soft_fail.append(st["name"])
                    el = self._took(st, t_step)
                    log(f"⚠ {st['name']} 未完成（非致命，前面的工作已生效，用时 {el}）",
                        "warn", st["id"])
                    continue

                self.results[st["id"]] = "fail"
                BUS.publish("step_end", step=st["id"], ok=False)
                all_ok = False
                el = self._took(st, t_step)
                log(f"✗ {st['name']} 失败（用时 {el}）", "error", st["id"])
                if mode == "auto":
                    log("自动模式：出错即停止，后续步骤不再执行", "warn")
                    break

            if soft_fail:
                log(f"以下步骤需要你处理一下：{'、'.join(soft_fail)}", "warn")
            if self.timings:
                # 一轮跑完把各步骤耗时按从慢到快列一遍：下次觉得慢，
                # 看这一行就知道该查哪一步，不用再拿时间戳做减法。
                slow = sorted(self.timings.items(), key=lambda kv: -kv[1])
                name_of = {s["id"]: s["name"] for s in PIPELINE}
                parts = [f"{name_of.get(k, k)} {_fmt_dur(v)}" for k, v in slow]
                log("各步骤耗时（从慢到快）：" + " ｜ ".join(parts), "info")
                log(f"本轮合计 {_fmt_dur(sum(self.timings.values()))}", "info")
        except BaseException as exc:
            # 兜底：无论如何都要把状态复位，否则界面会永远卡在「运行中」
            log(f"运行器异常终止: {exc!r}\n{traceback.format_exc()}", "error")
            all_ok = False
        finally:
            self._finish(all_ok, stopped=stopped)

    def _took(self, st, t_step):
        """记下这一步跑了多久，返回给人看的字符串。

        只做记时，不参与任何判断 —— 纯粹是为了「这一轮到底慢在哪一步」
        能有一条可查的证据，不用再去日志里按时间戳减法。
        """
        el = time.monotonic() - t_step
        self.timings[st["id"]] = el
        return _fmt_dur(el)

    def _finish(self, ok, stopped=False):
        self.running = False
        self.current = None
        RECORDER.close(ok)
        if stopped:
            # 中途停止是用户的正常操作，不能写成「有失败项」吓人
            log("===== 已被用户停止（已完成的步骤都已生效）=====", "warn")
        else:
            log("===== 全部完成 =====" if ok else "===== 结束（有失败项）=====",
                "head" if ok else "error")
        BUS.publish("run_end", ok=bool(ok), stopped=bool(stopped))

    def stop(self):
        request_stop()
        log("收到停止请求，正在中断当前命令…", "warn")


RUNNER = Runner()


# ══════════════════════════════════════════════════════════════
#  只读查询（给前端展示）
# ══════════════════════════════════════════════════════════════
def q_status():
    cfg = load_config()
    out = {"runner": RUNNER.snapshot(), "docker": None, "syncthing": None,
           "library": None, "config_path": CONFIG}
    try:
        p = subprocess.run([DOCKER, "ps", "--format", "{{.Names}}\t{{.Status}}"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=15)
        out["docker"] = {"ok": p.returncode == 0,
                         "containers": [l for l in (p.stdout or "").splitlines() if l]}
    except Exception as exc:
        out["docker"] = {"ok": False, "error": str(exc)[:120]}

    try:
        import urllib.request
        req = urllib.request.Request(
            cfg["sync"]["syncthing_gui"] + "/rest/system/connections",
            headers={"X-API-Key": cfg["sync"]["api_key"]})
        with urllib.request.urlopen(req, timeout=10) as r:
            conn = json.loads(r.read().decode())
        pc = conn["connections"].get(cfg["sync"]["phone_device"], {})
        out["syncthing"] = {"ok": True, "running": True,
                            "phone_connected": bool(pc.get("connected")),
                            "phone_address": pc.get("address") or ""}
        req2 = urllib.request.Request(
            cfg["sync"]["syncthing_gui"] +
            f"/rest/db/completion?device={cfg['sync']['phone_device']}"
            f"&folder={cfg['sync']['folder_id']}",
            headers={"X-API-Key": cfg["sync"]["api_key"]})
        with urllib.request.urlopen(req2, timeout=10) as r:
            comp = json.loads(r.read().decode())
        out["syncthing"].update({
            "completion": round(comp.get("completion", 0), 2),
            "need_items": comp.get("needItems"),
            "need_mb": round(comp.get("needBytes", 0) / 1048576, 1),
        })
    except Exception as exc:
        out["syncthing"] = {"ok": False, "running": False,
                            "error": str(exc)[:120]}

    dl = cfg["sync"]["downloads_dir"]
    if os.path.isdir(dl):
        files = [f for f in os.listdir(dl)
                 if os.path.splitext(f)[1].lower() in
                 (".flac", ".mp3", ".m4a", ".ogg", ".wav")]
        total = sum(os.path.getsize(os.path.join(dl, f)) for f in files)
        out["library"] = {"count": len(files),
                          "gb": round(total / 1073741824, 2)}
    else:
        out["library"] = {"count": 0, "gb": 0, "error": "目录不存在"}
    return out


def q_runs():
    items = []
    if os.path.isdir(LOGS):
        for fn in sorted(os.listdir(LOGS), reverse=True)[:60]:
            if not fn.endswith(".log"):
                continue
            p = os.path.join(LOGS, fn)
            items.append({"name": fn, "size": os.path.getsize(p),
                          "mtime": datetime.fromtimestamp(
                              os.path.getmtime(p)).strftime("%m-%d %H:%M:%S")})
    return items


def q_run_content(name):
    # 防目录穿越
    name = os.path.basename(name)
    p = os.path.join(LOGS, name)
    if not os.path.exists(p):
        return {"error": "不存在"}
    with open(p, encoding="utf-8", errors="replace") as fh:
        return {"name": name, "content": fh.read()[-200000:]}


def q_library_stats(force=False):
    """扫描曲库，统计规格分布。

    为什么要缓存：每个文件都要 ffprobe，260 个文件约 90 秒。
    结果按「文件名+大小+修改时间」缓存到文件，只有磁盘变化时才重扫。
    """
    cache_path = os.path.join(BASE, "library_stats.json")
    dl = load_config()["sync"]["downloads_dir"]
    if not os.path.isdir(dl):
        return {"ok": False, "error": f"目录不存在: {dl}"}

    files = []
    for f in sorted(os.listdir(dl)):
        p = os.path.join(dl, f)
        if not os.path.isfile(p):
            continue
        if os.path.splitext(f)[1].lower() not in (".flac", ".mp3", ".m4a", ".ogg", ".wav"):
            continue
        try:
            st = os.stat(p)
            files.append({"name": f, "size": st.st_size,
                          "mtime": int(st.st_mtime)})
        except OSError:
            pass

    # 指纹：文件集 + 大小，用来判断是否需要重扫
    sig = f"{len(files)}:" + ",".join(
        f"{x['name']}:{x['size']}" for x in files[:1000])

    if not force and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            if cached.get("sig") == sig:
                cached["cached"] = True
                return cached
        except Exception:
            pass

    # 需要实扫：借 go-music-dl 的 ffprobe（宿主机 ffprobe 直接读本地文件也行）
    probe = os.path.join(MONITOR, "")  # 占位，实际用宿主机 ffprobe
    results = []
    for i, x in enumerate(files, 1):
        p = os.path.join(dl, x["name"])
        try:
            r = subprocess.run(
                [FFPROBE, "-v", "quiet", "-select_streams", "a:0",
                 "-show_entries",
                 "stream=codec_name,sample_rate,bit_depth,bits_per_raw_sample,channels",
                 "-of", "json", p],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30)
            j = json.loads(r.stdout or "{}")
            st0 = (j.get("streams") or [{}])[0]
            results.append({
                "name": x["name"],
                "size": x["size"],
                "codec": st0.get("codec_name"),
                "rate": int(st0["sample_rate"]) if st0.get("sample_rate") else None,
                "bits": int(st0.get("bits_per_raw_sample")
                            or st0.get("bit_depth") or 0) or None,
                "ch": st0.get("channels"),
            })
        except Exception:
            results.append({"name": x["name"], "size": x["size"],
                            "codec": None, "rate": None, "bits": None, "ch": None})

    def dist(key):
        c = {}
        for r in results:
            v = r.get(key)
            k = "未知" if v is None else str(v)
            c[k] = c.get(k, 0) + 1
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    # 「真 Hi-Res」= 24bit 且 >= 88.2kHz
    true_hires = sum(1 for r in results
                     if (r.get("bits") or 0) >= 24 and (r.get("rate") or 0) >= 88200)

    out = {
        "ok": True, "cached": False,
        "count": len(results),
        "total_gb": round(sum(r["size"] for r in results) / 1073741824, 2),
        "true_hires": true_hires,
        "by_rate": dist("rate"),
        "by_bits": dist("bits"),
        "by_ch": dist("ch"),
        "by_codec": dist("codec"),
        "unreadable": sum(1 for r in results if r.get("codec") is None),
        "sig": sig,
        "files": results if force else [],
    }
    try:
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return out


# ══════════════════════════════════════════════════════════════
#  运行逻辑说明（存后端文件，不存浏览器 —— 换浏览器/清缓存都不丢）
# ══════════════════════════════════════════════════════════════
LOGIC_FILE = os.path.join(BASE, "logic.json")

LOGIC_DEFAULTS = {
    "overview": """这是一个把「酷狗『我喜欢』歌单 → 本机 Hi-Res 曲库 → 手机」整条链路包起来的本地应用。

原先这些流程散落在多个脚本里，靠记忆和手敲命令调用。现在合成一个界面：
  · 省心模式：连上热点后点一下「同步」，全流程自动跑完
  · 观察模式：每一步单独点，用来定位问题

两种模式共用同一份流水线定义，所以手动跑的和自动跑的是同一套逻辑。""",

    "projects": """Docker Desktop        —— 容器运行时，下载引擎跑在里面
go-music-dl           —— 下载引擎容器（提供 ffmpeg / ffprobe，以及网络出口）
music-monitor         —— 流程脚本容器（跑 Python 逻辑，存暂存与状态）
Syncthing             —— 文件同步，把曲库推到手机
FFmpeg / ffprobe      —— 探测音频真实规格（采样率 / 位深 / 声道 / 时长）
mutagen               —— 读写音频内嵌标签（歌手 / 歌名）
Chromaprint + fpcalc  —— 音频指纹，用来验证「这段音频到底是不是这首歌」
AcoustID              —— 指纹比对数据库，查音频的真实曲目身份
tag_files.py          —— 自建，按文件头判断容器格式后写标签
samplerate_guard      —— 自建，拦截会伤害设备的音频属性
check_integrity.py    —— 自建，检查文件是否损坏、标签是否完整
library-manager.ps1   —— 自建，落盘 / 去重 / 精简的统一入口""",

    "pipeline": """① 启动 Docker
   拉起 Docker Desktop 并等引擎就绪（首次约 30-60 秒）。

② 启动容器
   启动 go-music-dl（下载引擎 + ffmpeg）与 music-monitor（Python 流程宿主）。

③ 启动 Syncthing
   拉起同步服务，等本地 GUI 就绪。

④ 检查并下载新收藏
   读酷狗歌单 → 与基线（baseline.txt）比对，只处理新增收藏 →
   按设置里的音质规则挑曲目 → 取下载直链 → 下载到容器暂存。
   关键：付费曲目必须用 audio_group_id 作 album_audio_id，否则服务端报参数错误。

⑤ 解码加密音乐
   把 ncm / qmc / kgm 还原成普通 flac / mp3。
   纯本机运算，不联网；解出来的文件名换成真实歌名，原加密文件删除。
   没有加密文件时这一步直接跳过。

⑥ 落盘与整理
   取暂存到本机 → 采样率闸门 → 拷进同步文件夹 → 同名去重（按实测规格择优）
   → 补写 / 修正内嵌标签。
   注意：写标签必须在落盘之后，否则会被暂存副本覆盖。
   注意：这一步最慢（约 2 分钟），瓶颈是把容器里 11 GB 暂存拷到本机。
   注意：拷之前先比大小和 SHA256，内容相同的直接跳过、不碰 mtime ——
   这是「同步不重传」的关键，详见下面「回写暂存」一节。

⑦ 封面与歌词
   给缺封面的歌联网补专辑图，顺带补歌词，两者都写进音频容器。
   已存在且正确的歌完全不动（先读标签判断，再决定要不要写）。
   「这首歌查过、确实没有」这类**程序自己的状态**记在
   covers_state.json，不再为了记状态去改写音频文件。

⑧ 回写暂存
   把⑥⑦改过的文件同步回暂存目录（`_staging` 与它的上游 `hires_staging`）。
   为什么必须有这一步：⑥ 每次运行开头会先拿上游暂存覆盖本机暂存，
   不回写的话，⑥⑦在曲库里的成果会在下一轮被暂存版本原样盖回去 ——
   封面、歌词、规范化过的标签全部消失，再又被⑦重新下载嵌一遍，
   循环往复。回写让下一轮⑥判定「内容未变」而跳过，循环就此断开。
   实测：不回写时一次⑥抹掉 84 个封面、82 份歌词、152 个文件（7.07 GB）
   被判「内容不同」；回写之后连续两轮都是「落地 0 个 / 跳过 238 个」。

⑨ 安全检查
   逐文件探测采样率与声道，命中拦截名单的：删暂存 + 删曲库同名文件 + 加入屏蔽名单。
   必须在落盘之前跑，否则坏文件已经进了同步目录。

⑩ 曲库体检
   检查文件是否损坏、标签是否完整、容器与扩展名是否一致。

⑪ 同步到手机
   触发 Syncthing 扫描，先快速判断手机是否在线，再盯进度。
   手机离线时不会死等，会立刻报错并给出排查建议（此步为「非致命」）。""",

    "safety": """【为什么要有安全闸门】
实测：176.4kHz 采样率的文件在「手机 + USB DAC」链路上会输出极大噪声，
必须拔耳机才能停止。同设备播放 192kHz / 96kHz / 44.1kHz 全部正常。
已排除块大小、响度、峰值、DC 偏移、数字尖刺等因素，确认是该采样率档位的问题。
所以 176400 Hz 进了拦截名单 —— 不下、不放行、并屏蔽。

【三道防线】
1. 属性闸门：拦截会伤害设备的属性（采样率、声道数，可在设置里增删）
2. 屏蔽名单：平台无音源的曲目不再反复重试；内容错误的曲目不再重复下载
3. 不降级：拿不到无损以上就跳过，绝不用有损文件顶替（除非你在设置里明确允许）

【数据一致性】
· 落盘时先删同名异扩展名文件，避免「X.mp3 与 X.flac 并存」
· 去重按 ffprobe 实测规格择优，不看扩展名（酷狗的扩展名不可信）
· 写标签按文件头判断格式（大量文件是「伪装成 .mp3 的 FLAC」）

【音频指纹验证】
用 fpcalc 生成指纹 + AcoustID 比对，可以客观判断文件的音频内容
是否真的是文件名所指的那首歌。曾用这个方法查出 3 首「标签对但内容错」的曲目。""",

    "trouble": """【同步卡在 99% 不动】
多半是「幽灵文件」：某个文件在电脑上被删了，但同步索引还留着，
手机反复去拉一个已经不存在的东西。
处理：确认该文件确实已删 → 触发一次扫描 → 索引清掉后即恢复。

【手机反复掉线】
Android 的电池优化会杀掉 Syncthing。需要在系统设置里给它「无限制 / 不优化」，
并把 App 加入省电白名单。这是掉线的最常见原因。

【电脑删歌，手机不删】
同步文件夹是 sendonly（只发不收）模式，删除不会传播。
想彻底一致就清空手机文件夹重传；否则手机上需要手动删。

【下载报 status=2】
取流参数不对。付费曲目要用 audio_group_id 作 album_audio_id。

【第 5 步报「安全中止：Hi-Res 只有 N 首（目标 M）」】
传给 library-manager 的目标数量比暂存实际数量大太多，被它误判成「下载未完成」。
本应用已加自动钳制（用暂存实际数量当目标），正常不会再出现。

【Cookie 过期】
症状是「未取到曲目」。需要重新从酷狗客户端提取 Cookie 并写入容器。

【搜不到无损】
平台确实没有该曲目的无损版本。这类曲目会进屏蔽名单，不再重试。""",
}


def q_logic():
    saved = {}
    if os.path.exists(LOGIC_FILE):
        try:
            with open(LOGIC_FILE, encoding="utf-8") as fh:
                saved = json.load(fh)
        except Exception:
            saved = {}
    out = {}
    for k, default in LOGIC_DEFAULTS.items():
        out[k] = saved.get(k, default)
    return {"sections": out, "path": LOGIC_FILE,
            "edited": bool(saved)}


def save_logic(sections):
    with open(LOGIC_FILE, "w", encoding="utf-8") as fh:
        json.dump(sections, fh, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════
#  手动导入
# ══════════════════════════════════════════════════════════════
def finish_imported(cfg):
    """手动导入后的收尾：解码 → 封面与歌词 → 安全检查

    为什么要有这个
    --------------
    「不能只让自动下载的歌有这些待遇」。手动导入和自动下载落到的是同一个
    同步文件夹，用户看不出区别，所以后续处理也必须一样：

      · 解码  —— 万一曲库里还有漏网的加密文件（比如同步过来的）
      · 封面  —— 手动导入的歌同样没有封面，不补的话专辑墙一片灰
      · 安全检查 —— 手动挑的文件更可能带 176.4kHz 这种会炸耳的规格，
                    这一步本来就该在同步到手机之前拦住它
      · 回写暂存 —— **必须放在最后一步**，而且中间要有⑥。原因：
                    ⑤解码和⑦封面改的是**曲库**，暂存那边还是原始版本；
                    不回写的话下一轮⑥落盘会判定「内容不同」整份覆盖，
                    把刚补好的封面/歌词全抹掉。
                    而⑥自己也会改曲库（阶段 5 按文件名规范化标签，实测
                    每次会写 2~71 个文件）。所以顺序必须是
                        ⑥ 先把该改的都改完  ->  回写  ->  扫描
                    先回写再跑⑥的话，⑥改完的那几个又成了新的分叉，
                    下一轮照样被覆盖（2026-10-15 实测：⑨先跑、回写放中间时
                    2 个强制修正文件每轮都被重写）。
                    （2026-10-15 加，完整数据见 s_coverback 上面那段注释）

    每一步都是 soft：出错只记日志，不影响「文件已经导入了」这个事实。
    """
    for sid in ("decode", "covers", "verify", "maintain", "coverback"):
        step = next((s for s in PIPELINE if s["id"] == sid), None)
        if not step:
            continue
        log(f"—— 导入后收尾：{step['name']} ——", "head")
        try:
            step["fn"](cfg)
        except Exception:                       # noqa: BLE001
            log(f"{step['name']} 出错（不影响已导入的文件）:\n"
                + traceback.format_exc(), "warn")


def _drop_staging_same_name(stems, log=print):
    """替换模式：把暂存目录里同名的旧副本也删掉。

    为什么必须删
    ------------
    `hires_staging` / `_staging` 是 ⑥落盘的数据源。只在曲库里替换的话，
    下一轮 ⑥ 会拿暂存里那份**旧的错歌**再落盘一次，替换成果当场被冲掉，
    用户看到的就是「刚换完，一跑流程又变回错的」。
    """
    want = {_imp._norm_stem(s) for s in stems if s}
    if not want:
        return 0
    gone = 0
    for d in (HIRES_STAGING, LOCAL_STAGING):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            base, e = os.path.splitext(fn)
            if e.lower() not in _imp.AUDIO_EXT:
                continue
            if _imp._norm_stem(base) not in want:
                continue
            try:
                os.remove(os.path.join(d, fn))
                gone += 1
                log(f"  已删除暂存里的同名旧副本: {fn}")
            except OSError as exc:
                log(f"  ⚠ 暂存旧副本删不掉: {fn}（{exc}）")
    return gone


def run_import(mode, replace=False):
    """mode: 'files' 选文件 | 'folder' 选文件夹

    replace=True 是**替换模式**：曲库里同名的旧文件会被这次导入的这份
    替换并删除，不看规格高低。用于「自动下载下错了歌、手工换一份」的场景。

    导入后走与自动下载同样的后续：拷进同步文件夹 → 打标签 → 触发同步扫描。
    """
    if RUNNER.running:
        log("已有任务在跑，等它结束再导入", "warn")
        return

    cfg = load_config()
    title = (("替换导入" if replace else "导入歌曲")
             + ("（文件夹）" if mode == "folder" else "（文件）"))
    RECORDER.start(title)
    log(f"===== {title} =====", "head")
    if replace:
        log("【替换模式】曲库里同名的旧文件会被这份替换并删除，不看规格高低", "info")
    BUS.publish("import_start", mode=mode)
    try:
        paths = _imp.pick(mode, PICKER)
        if not paths:
            log("未选择任何内容（或对话框被取消）", "warn")
            return
        for p in paths:
            log(f"  选中: {p}")

        stat = _imp.import_songs(
            paths, cfg, FFPROBE, cfg["advanced"]["tag_files_py"],
            log=lambda m: log(m, "out"),
            decoder=cfg["advanced"].get("music_decode_exe"),
            replace=replace)
        if stat.get("ok"):
            # 替换模式：先把暂存里的同名旧副本清掉，再跑收尾。
            # 顺序不能反 —— finish_imported 里 ⑥落盘是从暂存往曲库拷的，
            # 暂存要是还留着旧歌，落盘会把它重新写回曲库。
            if replace and stat.get("imported"):
                n = _drop_staging_same_name(
                    stat["imported"], log=lambda m: log(m, "out"))
                log(f"  暂存目录清理：{n} 个同名旧副本", "info")
            # 先做收尾处理，**最后**才通知 Syncthing 扫描（2026-10-15 改）
            # ------------------------------------------------------------------
            # 顺序反过来过：原来是先 trigger_scan() 再 finish_imported()，
            # 而 finish_imported 里那三步（解码 / 封面 / 安全检查）每一个都会
            # 改写曲库里的音频文件。结果就是同一首歌在一轮里被改了两次 ——
            # 先按「刚导入」的样子同步一遍，改完再同步一遍，
            # 手机会看到同一个文件被重传两次，用户那边就表现为
            # 「库一改就要求重新同步」。
            #
            # 现在改成：所有会动文件的步骤全部跑完 -> 库不再变化 -> 再触发扫描。
            # 这一步之后整轮流程不会再写曲库，所以扫描看到的就是最终状态，
            # 同步一次就够。
            finish_imported(cfg)
            _imp.trigger_scan(cfg, log=lambda m: log(m, "info"))
            log(f"✓ 导入完成，{stat['ok']} 首已进入同步文件夹", "ok")
        else:
            log("没有新增文件（全部跳过或失败）", "warn")
        # 曲库统计缓存作废，下次打开「曲库」页会重扫
        try:
            p = os.path.join(BASE, "library_stats.json")
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    except Exception:
        log("导入过程出错：\n" + traceback.format_exc(), "error")
    finally:
        RECORDER.close()
        BUS.publish("import_end")


def q_blocked():
    """读取容器里的 blocked 名单"""
    try:
        p = subprocess.run(
            [DOCKER, "exec", "music-monitor", "python", "-c",
             "import sys;sys.path.insert(0,'/app');"
             "import importlib.util;"
             "s=importlib.util.spec_from_file_location('ml','/app/manage_library.py');"
             "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
             "print('\\n'.join(sorted(m.load_blocked())))"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
        items = [l for l in (p.stdout or "").splitlines() if l.strip()]
        return {"ok": True, "items": items, "count": len(items)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120], "items": []}


# ══════════════════════════════════════════════════════════════
#  HTTP 服务
# ══════════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        """屏蔽浏览器/SSE 断开产生的连接重置噪音。

        默认实现会把 ConnectionResetError 打到 stderr，刷屏且无意义
        —— 前端刷新页面、SSE 重连都会触发。
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True

    # ---------- 工具 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        p = u.path

        if p == "/":
            return self._file(os.path.join(STATIC, "index.html"),
                              "text/html; charset=utf-8")
        if p.startswith("/static/"):
            rel = p[len("/static/"):]
            fp = os.path.normpath(os.path.join(STATIC, rel))
            if not fp.startswith(STATIC):
                self.send_error(403)
                return
            ext = os.path.splitext(fp)[1].lower()
            ct = {".js": "application/javascript; charset=utf-8",
                  ".css": "text/css; charset=utf-8",
                  ".html": "text/html; charset=utf-8",
                  ".svg": "image/svg+xml",
                  ".png": "image/png"}.get(ext, "application/octet-stream")
            return self._file(fp, ct)

        if p == "/api/pipeline":
            return self._json({"steps": [
                {"id": s["id"], "name": s["name"], "desc": s["desc"],
                 "critical": bool(s.get("critical")),
                 "stop_warning": s.get("stop_warning") or ""}
                for s in PIPELINE]})
        if p == "/api/status":
            return self._json(q_status())
        if p == "/api/config":
            return self._json({"config": load_config(), "path": CONFIG})
        if p == "/api/runs":
            return self._json({"runs": q_runs()})
        if p == "/api/run":
            return self._json(q_run_content(qs.get("name", [""])[0]))
        if p == "/api/blocked":
            return self._json(q_blocked())
        if p == "/api/library_stats":
            force = qs.get("force", ["0"])[0] in ("1", "true")
            return self._json(q_library_stats(force=force))
        if p == "/api/logic":
            return self._json(q_logic())
        if p == "/api/events":
            return self._sse()
        self.send_error(404)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        body = self._body()

        if p == "/api/run":
            mode = body.get("mode", "auto")
            only = body.get("step")
            if mode == "auto":
                threading.Thread(target=RUNNER.run, args=("auto",),
                                 daemon=True).start()
            else:
                if not only:
                    return self._json({"ok": False, "error": "缺少 step"}, 400)
                threading.Thread(target=RUNNER.run, args=("manual", only),
                                 daemon=True).start()
            return self._json({"ok": True})
        if p == "/api/stop":
            RUNNER.stop()
            return self._json({"ok": True})
        if p == "/api/import":
            mode = body.get("mode", "files")
            replace = bool(body.get("replace"))
            if mode not in ("files", "folder"):
                return self._json({"ok": False, "error": "mode 无效"}, 400)
            if _imp is None:
                return self._json({"ok": False, "error": "导入模块未加载"}, 500)
            threading.Thread(target=run_import, args=(mode, replace),
                             daemon=True).start()
            return self._json({"ok": True, "replace": replace})
        if p == "/api/config":
            cfg = body.get("config")
            if not isinstance(cfg, dict):
                return self._json({"ok": False, "error": "config 无效"}, 400)
            save_config(cfg)
            log("配置已保存", "ok")
            return self._json({"ok": True, "config": load_config()})
        if p == "/api/logic":
            sections = body.get("sections")
            if not isinstance(sections, dict):
                return self._json({"ok": False, "error": "sections 无效"}, 400)
            save_logic(sections)
            log("运行逻辑说明已保存", "ok")
            return self._json({"ok": True})
        self.send_error(404)

    # ---------- SSE ----------
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = BUS.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    data = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(q)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Music Flow 已启动: {url}")
    print(f"配置文件: {CONFIG}")
    print(f"运行日志: {LOGS}")
    print("按 Ctrl+C 退出")
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
