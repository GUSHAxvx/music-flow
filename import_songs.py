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
import shutil
import subprocess
import sys
import urllib.request
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
PICKER = os.path.join(BASE, "scripts", "pick_files.ps1")

# 能导入的音频扩展名
AUDIO_EXT = (".flac", ".mp3", ".m4a", ".ogg", ".wav", ".ape", ".wma", ".aac")
# 无损容器（按扩展名初判，最终以 ffprobe 实测为准）
LOSSLESS_EXT = (".flac", ".wav", ".ape", ".alac")

_ffprobe_cache = {}


def _load_tag_module(tag_py):
    """动态加载 tag_files.py，拿它的打标签实现"""
    spec = importlib.util.spec_from_file_location("tagmod", tag_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pick(mode, picker=None):
    """弹出系统对话框，返回选中的路径列表（取消则返回空列表）"""
    script = picker or PICKER
    if not os.path.exists(script):
        return []
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", script, "-Mode", mode],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600)
        out = (r.stdout or "").strip()
        return [l.strip() for l in out.splitlines() if l.strip()]
    except Exception:
        return []


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


def collect(paths):
    """把「文件或文件夹」的混合列表展开成音频文件列表"""
    out = []
    for p in paths:
        if os.path.isfile(p):
            if os.path.splitext(p)[1].lower() in AUDIO_EXT:
                out.append(p)
        elif os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for f in sorted(files):
                    if os.path.splitext(f)[1].lower() in AUDIO_EXT:
                        out.append(os.path.join(root, f))
    return out


def import_songs(paths, cfg, ffprobe, tag_py, log=print):
    """执行导入。返回统计 dict"""
    lib = cfg["sync"]["downloads_dir"]
    accept_lossy = bool(cfg.get("import", {}).get("accept_lossy", True))
    block_rates = set(cfg.get("guard", {}).get("block_sample_rates") or [])
    block_ch = set(cfg.get("guard", {}).get("block_channels") or [])

    os.makedirs(lib, exist_ok=True)
    files = collect(paths)
    log(f"共选中 {len(paths)} 项，展开为 {len(files)} 个音频文件")
    if not files:
        return {"total": 0, "ok": 0, "skip": 0, "fail": 0}

    tagmod = None
    try:
        tagmod = _load_tag_module(tag_py)
    except Exception as exc:
        log(f"⚠ 打标签模块加载失败（将跳过打标签）: {exc}", )

    stat = {"total": len(files), "ok": 0, "skip": 0, "fail": 0, "tagged": 0}
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

        # ---- 3. 同名去重：按实测规格择优 ----
        dst = os.path.join(lib, name)
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
            os.replace(tmp, dst)          # 原子替换，避免同步到半个文件
        except Exception as exc:          # noqa: BLE001
            log(f"  [{i}/{len(files)}] ✗ 拷贝失败: {exc}")
            stat["fail"] += 1
            continue

        spec = (f"{info['rate'] / 1000:g}kHz"
                + (f"/{info['bits']}bit" if info.get("bits") else "")
                + f" {info['codec']}")
        log(f"  [{i}/{len(files)}] ✓ 已导入 ({spec}) {name}")
        stat["ok"] += 1

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
        + (f"，已写标签 {stat['tagged']}" if stat["tagged"] else ""))
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
