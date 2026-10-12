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
import json
import os
import queue
import re
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
        "max_albums": 400,             # Hi-Res 目标数量（有基线后不再强制上限）
        "lossless_target": 30,         # 无损目标数量
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
    "advanced": {
        "note": "依赖的开源项目与脚本（一般不用改）",
        "library_manager": r"D:\music-monitor\library-manager.ps1",
        "manage_library_py": "/app/manage_library.py",
        "tag_files_py": r"D:\music-monitor\tag_files.py",
        "check_integrity_py": r"D:\music-monitor\check_integrity.py",
        "samplerate_guard_py": r"D:\music-monitor\samplerate_guard_host.py",
        "audit_quality_ps1": r"D:\music-monitor\audit_quality.ps1",
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

    同时做两件事：
      1) 推给前端（实时显示）
      2) 写进本次运行的日志文件（事后排查）
    —— 只推前端不写文件的话，出问题后翻 run 日志会看不到任何线索。
    """
    text = str(msg)
    BUS.publish("log", message=text, level=level, step=step)
    try:
        RECORDER.write(f"[{level}] {text}" if level != "out" else text)
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


# 当前正在运行的子进程 + 停止信号。
# 「停止」要真的能中断，而不是等当前步骤跑完 —— 否则用户只能干等。
_CURRENT_PROC = None
_STOP_EVENT = threading.Event()


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
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env or os.environ.copy())
    except FileNotFoundError as exc:
        msg = f"命令不存在: {args[0]}  ({exc})"
        log(msg, "error", step)
        RECORDER.write("!! " + msg)
        return False, msg

    _CURRENT_PROC = p
    lines = []
    start = time.time()
    killed = False
    try:
        for line in p.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            lvl = "error" if looks_like_error(line) else "out"
            log(line, lvl, step)
            RECORDER.write(line)
            if _STOP_EVENT.is_set():
                killed = True
                break
            if timeout and time.time() - start > timeout:
                p.kill()
                log("超时，已终止", "error", step)
                break
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
    # 把「哪些音质可下载」的策略用环境变量传进容器。
    # 注意：docker exec 默认不继承宿主机的环境变量，必须显式 -e 传入。
    policy = {
        "accept_hires": bool(d.get("accept_hires", True)),
        "accept_lossless": bool(d.get("accept_lossless", True)),
        "accept_lossy": bool(d.get("accept_lossy", False)),
        "min_bitrate": int(d.get("min_bitrate") or 0),
    }
    args = [DOCKER, "exec",
            "-e", "DOWNLOAD_POLICY=" + json.dumps(policy, ensure_ascii=False),
            "-e", f"LIBRARY_DIR={CONTAINER_LIB}",
            "music-monitor", "python", cfg["advanced"]["manage_library_py"],
            "--workers", "4",
            "--target", str(d["max_albums"]),
            "--lossless-target", str(d["lossless_target"]),
            "--sort", d["sort_mode"]]
    log(f"下载策略: Hi-Res={policy['accept_hires']} "
        f"无损={policy['accept_lossless']} 有损={policy['accept_lossy']} "
        f"最低码率={policy['min_bitrate']}kbps")
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
    return ok


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
        time.sleep(5)

    log("等待超时（进度长时间未推进），同步可能未完成", "warn")
    return False


# 步骤表：两种模式共用的唯一真相
#   soft=True 表示「这一步失败不算致命」：
#   自动模式不会因此中止后续步骤，只是把该步标为待处理。
#   典型例子是同步 —— 手机没连上时前面所有工作其实都做完了，
#   不该让整次运行显示成失败。
PIPELINE = [
    {"id": "docker",     "name": "① 启动 Docker",      "desc": "拉起 Docker Desktop 并等引擎就绪", "fn": s_docker_start},
    {"id": "containers", "name": "② 启动容器",          "desc": "启动 go-music-dl / music-monitor",  "fn": s_containers_start},
    {"id": "syncthing",  "name": "③ 启动 Syncthing",    "desc": "拉起同步服务并等 GUI 就绪",          "fn": s_syncthing_start},
    {"id": "download",   "name": "④ 检查并下载新收藏",  "desc": "按设置里的音质规则抓取缺失曲目",     "fn": s_download},
    {"id": "maintain",   "name": "⑤ 落盘与整理",        "desc": "取暂存→闸门→落盘→去重→写标签",      "fn": s_maintain},
    {"id": "verify",     "name": "⑥ 安全检查",          "desc": "拦截会炸耳的采样率，清理暂存",       "fn": s_verify},
    {"id": "integrity",  "name": "⑦ 曲库体检",          "desc": "检查文件是否损坏、标签是否完整",     "fn": s_integrity},
    {"id": "sync",       "name": "⑧ 同步到手机",        "desc": "触发扫描并盯着传到完成",             "fn": s_sync,
     "soft": True},
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
        self.results = {}          # step_id -> ok/warn/fail/skip

    def snapshot(self):
        return {
            "running": self.running,
            "mode": self.mode,
            "current": self.current,
            "results": dict(self.results),
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
        try:
            for st in steps:
                if _STOP_EVENT.is_set():
                    log("已被用户停止", "warn")
                    self.results[st["id"]] = "skip"
                    all_ok = False
                    break
                self.current = st["id"]
                BUS.publish("step_start", step=st["id"], name=st["name"])
                log(f"───── {st['name']}：{st['desc']} ─────", "head", st["id"])
                try:
                    ok = st["fn"](cfg)
                except Exception:
                    log("步骤抛出异常：\n" + traceback.format_exc(), "error",
                        st["id"])
                    ok = False

                if ok:
                    self.results[st["id"]] = "ok"
                    BUS.publish("step_end", step=st["id"], ok=True)
                    log(f"✓ {st['name']} 完成", "ok", st["id"])
                    continue

                # 失败：软步骤（如手机没连上导致的同步失败）不算致命
                if st.get("soft"):
                    self.results[st["id"]] = "warn"
                    BUS.publish("step_end", step=st["id"], ok=False, soft=True)
                    soft_fail.append(st["name"])
                    log(f"⚠ {st['name']} 未完成（非致命，前面的工作已生效）",
                        "warn", st["id"])
                    continue

                self.results[st["id"]] = "fail"
                BUS.publish("step_end", step=st["id"], ok=False)
                all_ok = False
                log(f"✗ {st['name']} 失败", "error", st["id"])
                if mode == "auto":
                    log("自动模式：出错即停止，后续步骤不再执行", "warn")
                    break

            if soft_fail:
                log(f"以下步骤需要你处理一下：{'、'.join(soft_fail)}", "warn")
        except BaseException as exc:
            # 兜底：无论如何都要把状态复位，否则界面会永远卡在「运行中」
            log(f"运行器异常终止: {exc!r}\n{traceback.format_exc()}", "error")
            all_ok = False
        finally:
            self._finish(all_ok)

    def _finish(self, ok):
        self.running = False
        self.current = None
        RECORDER.close(ok)
        log("===== 全部完成 =====" if ok else "===== 结束（有失败项）=====",
            "head" if ok else "error")
        BUS.publish("run_end", ok=bool(ok))

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

⑤ 落盘与整理
   取暂存到本机 → 采样率闸门 → 拷进同步文件夹 → 同名去重（按实测规格择优）
   → 补写 / 修正内嵌标签。
   注意：写标签必须在落盘之后，否则会被暂存副本覆盖。
   注意：这一步最慢（约 2 分钟），瓶颈是把容器里 11 GB 暂存拷到本机。

⑥ 安全检查
   逐文件探测采样率与声道，命中拦截名单的：删暂存 + 删曲库同名文件 + 加入屏蔽名单。
   必须在落盘之前跑，否则坏文件已经进了同步目录。

⑦ 曲库体检
   检查文件是否损坏、标签是否完整、容器与扩展名是否一致。

⑧ 同步到手机
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
                {"id": s["id"], "name": s["name"], "desc": s["desc"]}
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
