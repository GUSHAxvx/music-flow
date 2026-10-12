#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采样率安全闸门（宿主机版）

放在哪里跑
----------
宿主机。原因：需要 docker 命令访问容器，而容器内没有 docker CLI。
宿主机有 Python 3.13 和 ffprobe，够用。

它拦什么
--------
采样率命中黑名单（默认 176400 Hz）的音频文件。

为什么拦
--------
实测：Swedish House Mafia、The Weeknd - Moth To A Flame.flac（176400 Hz）
用 Salt Player + 水月雨 ECHO-A 播放时，耳机爆出极大噪声，必须拔掉耳机才停。
同设备播放 192kHz / 96kHz / 44.1kHz 文件全部正常。
已排除块大小、响度、峰值、DC 偏移、数字尖刺等因素。
结论：**176.4kHz 这一档在本机手机链路上会输出噪声**。

工作方式
--------
1. 从容器 music-monitor 取暂存文件清单（/app/data/hires_staging）
2. 逐一把文件内容经 stdout 取出（避免 docker cp 的临时文件）
3. 用宿主机 ffprobe 探测采样率
4. 命中黑名单的：删暂存 + 删同步文件夹同名文件 + 记入 blocked

用法
----
    python samplerate_guard_host.py            # 干跑
    python samplerate_guard_host.py --apply    # 实际拦截
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

FFPROBE = r"C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe"
DOCKER = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
STAGING_CTR = "music-monitor"
LIBRARY_HOST = r"D:\go-music-dl\downloads"
CONTAINER_STAGING = "/app/data/hires_staging"
BLOCK_RATES = {176400}
AUDIO_EXT = (".flac", ".mp3", ".m4a", ".ogg", ".wav")
# 采样率缓存：键是文件名，值是 {"size":…, "rate":…, "ch":…}。
# 文件大小没变就复用上次结果，避免每次运行都全量重探（233 个文件约 5 分钟）。
CACHE_FILE = r"D:\music-monitor\samplerate_cache.json"

# 需要拦截的声道数（空集合 = 不按声道拦截）。
# 由应用通过环境变量 GUARD_POLICY 注入，例如 {"block_channels":[1,6,8]}。
BLOCK_CHANNELS = set()
try:
    _gp = json.loads(os.getenv("GUARD_POLICY") or "{}")
    if _gp.get("block_sample_rates"):
        BLOCK_RATES = {int(x) for x in _gp["block_sample_rates"]}
    if _gp.get("block_channels"):
        BLOCK_CHANNELS = {int(x) for x in _gp["block_channels"]}
except Exception:                                  # noqa: BLE001
    pass


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, **kw)


def list_staging():
    """从容器取暂存文件清单（含大小）"""
    code = (
        "import os,json\n"
        f"d='{CONTAINER_STAGING}'\n"
        "out=[]\n"
        "if os.path.isdir(d):\n"
        "    for f in sorted(os.listdir(d)):\n"
        "        p=os.path.join(d,f)\n"
        "        if os.path.isfile(p):\n"
        "            out.append({'name':f,'size':os.path.getsize(p)})\n"
        "print(json.dumps(out,ensure_ascii=False))\n"
    )
    r = sh([DOCKER, "exec", STAGING_CTR, "python", "-c", code],
           text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"  ⚠ 取清单失败: {(r.stderr or '')[:150]}")
        return []
    import json
    try:
        return json.loads((r.stdout or "").strip() or "[]")
    except Exception as exc:                            # noqa: BLE001
        print(f"  ⚠ 解析清单失败: {exc}")
        return []


def fetch_and_probe(name):
    """把容器里的文件取出到临时文件，用宿主机 ffprobe 读采样率与声道数。

    返回 ({"rate":..,"ch":..}, None) 或 (None, 错误说明)
    """
    src = f"{CONTAINER_STAGING}/{name}"
    r = sh([DOCKER, "exec", STAGING_CTR, "cat", src])
    if r.returncode != 0 or not r.stdout:
        return None, "读取失败"
    fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(name)[1])
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(r.stdout)
        p = sh([FFPROBE, "-v", "quiet", "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate,channels",
                "-of", "csv=p=0", tmp],
               text=True, encoding="utf-8", errors="replace")
        s = (p.stdout or "").strip()
        # 注意：某些 mp3 会返回 "44100,2," 这种带尾逗号的形式，需按段取
        parts = [x.strip() for x in s.split(",") if x.strip()]
        rate = next((int(x) for x in parts if x.isdigit()), None)
        # 声道数是第二个纯数字字段（第一个是采样率）
        nums = [int(x) for x in parts if x.isdigit()]
        ch = nums[1] if len(nums) >= 2 else None
        if rate is None:
            return None, f"ffprobe: {s[:40]!r}"
        return {"rate": rate, "ch": ch}, None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def load_blocked():
    """让容器内脚本帮忙读 blocked 名单"""
    code = (
        "import sys\n"
        "sys.path.insert(0,'/app')\n"
        "import importlib.util\n"
        "spec=importlib.util.spec_from_file_location('ml','/app/manage_library.py')\n"
        "ml=importlib.util.module_from_spec(spec); spec.loader.exec_module(ml)\n"
        "import json\n"
        "print(json.dumps(sorted(ml.load_blocked()),ensure_ascii=False))\n"
    )
    r = sh([DOCKER, "exec", STAGING_CTR, "python", "-c", code],
           text=True, encoding="utf-8", errors="replace")
    import json
    try:
        return json.loads((r.stdout or "").strip() or "[]")
    except Exception:                                   # noqa: BLE001
        return []


def save_blocked(items):
    import json
    payload = json.dumps(items, ensure_ascii=False)
    code = (
        "import sys,json\n"
        "sys.path.insert(0,'/app')\n"
        "import importlib.util\n"
        "spec=importlib.util.spec_from_file_location('ml','/app/manage_library.py')\n"
        "ml=importlib.util.module_from_spec(spec); spec.loader.exec_module(ml)\n"
        f"items=json.loads({payload!r})\n"
        "ml.save_blocked(set(items))\n"
        "print('saved', len(items))\n"
    )
    r = sh([DOCKER, "exec", STAGING_CTR, "python", "-c", code],
           text=True, encoding="utf-8", errors="replace")
    print(f"  {(r.stdout or '').strip()}")


def remove_from_staging(name):
    code = (
        "import os\n"
        f"p=os.path.join('{CONTAINER_STAGING}',{name!r})\n"
        "if os.path.exists(p):\n"
        "    os.remove(p); print('removed')\n"
        "else:\n"
        "    print('not found')\n"
    )
    r = sh([DOCKER, "exec", STAGING_CTR, "python", "-c", code],
           text=True, encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


def load_cache():
    """读取采样率缓存：{文件名: {"size": 字节数, "rate": 采样率}}"""
    import json
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:                               # noqa: BLE001
            return {}
    return {}


def save_cache(cache):
    import json
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="只检查前 N 个（调试用）")
    ap.add_argument("--no-cache", action="store_true", help="忽略缓存，全部重新探测")
    args = ap.parse_args()

    print(f"===== 音频属性安全闸门（宿主机版） apply={args.apply} =====")
    print(f"  拦截采样率: {sorted(BLOCK_RATES) or '(不拦)'}")
    print(f"  拦截声道数: {sorted(BLOCK_CHANNELS) or '(不拦)'}")

    entries = list_staging()
    if not entries:
        print("  暂存为空或不可读，跳过")
        return 0
    audio = [e for e in entries
             if os.path.splitext(e["name"])[1].lower() in AUDIO_EXT]
    print(f"  暂存文件 {len(entries)} 个，其中音频 {len(audio)} 个")
    if args.limit:
        audio = audio[:args.limit]
        print(f"  （限制只检查前 {len(audio)} 个）")

    # 缓存命中：文件大小没变就复用上次结果，避免每次全量重探
    cache = {} if args.no_cache else load_cache()
    hits = []            # (文件名, 原因描述)
    unprobed = []
    probed = 0
    reused = 0
    for i, e in enumerate(audio, 1):
        key = e["name"]
        size = e.get("size")
        c = cache.get(key)
        # 缓存可用条件：大小没变、有采样率，且（若启用声道拦截）也有声道数据。
        # 旧版缓存没有 ch 字段，若不检查就会让声道拦截静默失效 ——
        # 宁可多探一次，也不能让安全检查假装通过。
        cache_ok = bool(c and size is not None and c.get("rate")
                        and c.get("size") == size)
        if cache_ok and BLOCK_CHANNELS and c.get("ch") is None:
            cache_ok = False
        if cache_ok:
            info, err = {"rate": c["rate"], "ch": c.get("ch")}, None
            reused += 1
        else:
            if i % 25 == 0 or (BLOCK_CHANNELS and c and c.get("ch") is None):
                print(f"    探测进度 {probed} / {len(audio)}（缓存复用 {reused}）…")
            info, err = fetch_and_probe(key)
            probed += 1
            if info is not None:
                cache[key] = {"size": size, "rate": info["rate"],
                              "ch": info.get("ch")}
        if info is None:
            unprobed.append((key, err))
            continue

        reasons = []
        if BLOCK_RATES and info["rate"] in BLOCK_RATES:
            reasons.append(f"{info['rate']} Hz")
        if BLOCK_CHANNELS and info.get("ch") in BLOCK_CHANNELS:
            reasons.append(f"{info['ch']} 声道")
        if reasons:
            why = " / ".join(reasons)
            hits.append((key, why))
            print(f"    ⚠ 命中: {key}  ({why})")

    save_cache(cache)
    print()
    print(f"  结果: 命中 {len(hits)} 个；新探测 {probed} 个，缓存复用 {reused} 个，"
          f"未读到 {len(unprobed)} 个")
    for fn, err in unprobed[:8]:
        print(f"    未读到: {fn}  <- {err}")

    if not hits:
        print("  ✅ 没有需要拦截的文件")
        return 0

    print()
    print("=== 需拦截 ===")
    for fn, why in hits:
        print(f"    {fn}   ({why})")

    if not args.apply:
        print()
        print("  [dry-run] 未删除。加 --apply 执行。")
        return 0

    print()
    print("=== 执行拦截 ===")
    blocked = load_blocked()
    before = len(blocked)
    for fn, why in hits:
        base = os.path.splitext(fn)[0]
        r = remove_from_staging(fn)
        print(f"  暂存 {fn}: {r}   原因: {why}")
        for lf in os.listdir(LIBRARY_HOST):
            if os.path.splitext(lf)[0] == base:
                try:
                    os.remove(os.path.join(LIBRARY_HOST, lf))
                    print(f"  已从曲库删除: {lf}")
                except OSError as exc:
                    print(f"  删曲库文件失败 {lf}: {exc}")
        blocked.append(base)

    save_blocked(sorted(set(blocked)))
    print(f"  blocked: {before} -> {len(set(blocked))}")
    print("  拦截完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
