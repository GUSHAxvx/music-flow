#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""曲库守护：查「同一歌手的歌撞名」和「同一首音频存了两份」。

为什么要有这个脚本
------------------
2026-10-18 用户报了两件事：
  1. 同一个歌手的不同歌，在手机上显示成同一个名字；
  2. 曲库里藏了重复的副本、还有根本没收藏过的歌。

第 1 件的根因：七步里的「封面与歌词」在换用另一套名字搜到结果时，会把
**那一套名字**写回文件标签（scripts/fix_covers.py 的
`artist, title = pic_name or lrc_name or cands[0]`）。于是
`アメイジングツリーズ` 被写成了 `Amazing Trees`，个别文件甚至被写成了
同一歌手的**另一首歌**的名字。

这个脚本只做检测，默认一个字节都不改；加 --fix 才动手：
  --fix-tags  撞名的那些文件，按**文件名**把歌手/歌名改回来
              （文件名是下载时用户收藏里的原始名字，比搜索结果可信）
  --fix-dup   音频完全相同的副本，只保留收藏计划里那一首（plan.json 的 base），
              计划里都没有就留体积大的那份

用法
----
  python library_guard.py                 # 只报告（流水线⑩用这个）
  python library_guard.py --fix-tags      # 顺手把撞名的改回来
  python library_guard.py --fix-dup       # 顺手删掉重复副本
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, r"D:\music-monitor")
from tag_files import (AUDIO, apply_tags, open_audio, read_tags,  # noqa: E402
                       split_stem, _write_tags_unconditional)

DL = os.environ.get("LIBRARY_DIR", r"D:\go-music-dl\downloads")
PLAN = r"D:\music-monitor\_lists\plan.json"


def norm(s):
    """比较用的归一化：去掉空格、统一大小写。"""
    return re.sub(r"\s+", "", (s or "")).casefold()


def load_plan_bases():
    """收藏/下载计划里的 '歌手 - 歌名' 集合，用来判断该保留哪一份。"""
    if not os.path.exists(PLAN):
        return set()
    try:
        d = json.load(open(PLAN, encoding="utf-8"))
    except Exception:
        return set()
    bases = set()
    for key in ("hires", "lossless"):
        for it in d.get(key) or []:
            b = (it or {}).get("base")
            if b:
                bases.add(norm(b))
    return bases


def audio_md5(path):
    """只算音频流，忽略标签与内嵌封面。"""
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "quiet", "-i", path, "-map", "0:a", "-f", "md5", "-"],
            capture_output=True, text=True, timeout=120)
        m = re.search(r"MD5=([0-9a-f]{32})", out.stdout or "")
        return m.group(1) if m else None
    except Exception:
        return None


def stream_info(path):
    """(时长, 采样率, 位深, 声道) —— 用来给重复检测做便宜的预筛。"""
    f = open_audio(path)
    if f is None:
        return None
    try:
        info = f.info
        return (round(float(getattr(info, "length", 0) or 0), 1),
                getattr(info, "sample_rate", 0),
                getattr(info, "bits_per_sample", 0),
                getattr(info, "channels", 0))
    except Exception:
        return None


def write_tags_safely(path, artist, title):
    """优先原子替换；文件被别的进程占着（WinError 32/5）时退回就地写。

    实测：Syncthing 正在哈希某个文件时，`os.replace` 会报
    `[WinError 32] 另一个程序正在使用此文件`，但文件本身允许就地读写。
    """
    try:
        return apply_tags(path, artist, title)
    except PermissionError:
        _write_tags_unconditional(path, artist, title)
        return True


def scan():
    files = sorted(f for f in os.listdir(DL)
                   if os.path.splitext(f)[1].lower() in AUDIO)
    rows = []
    for fn in files:
        p = os.path.join(DL, fn)
        stem = os.path.splitext(fn)[0]
        fa, ft = split_stem(stem)
        ta, tt = read_tags(p)
        if ta is None:
            rows.append({"fn": fn, "path": p, "stem": stem, "fa": fa, "ft": ft,
                         "ta": "", "tt": "", "bad": True})
            continue
        rows.append({"fn": fn, "path": p, "stem": stem, "fa": fa, "ft": ft,
                     "ta": ta, "tt": tt, "bad": False})
    return rows


def check_collisions(rows, fix):
    """同一歌手 + 同一歌名 出现在多个文件里 —— 就是用户看到的现象。"""
    groups = {}
    for r in rows:
        if r["bad"] or not r["tt"].strip():
            continue
        artist = r["ta"].strip() or r["fa"]
        groups.setdefault((norm(artist), norm(r["tt"])), []).append(r)

    hits = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"\n[歌名撞车] 同一歌手出现同名歌曲：{len(hits)} 组")
    for (a, t), items in hits.items():
        print(f"  ⚠ 歌手={items[0]['ta'] or items[0]['fa'] or '?'}  歌名={items[0]['tt']}")
        for r in items:
            mark = "文件名一致" if norm(r["ft"]) == norm(r["tt"]) else "文件名不符"
            print(f"     - {r['fn']}   （{mark}）")
        if fix:
            for r in items:
                if norm(r["ft"]) and norm(r["ft"]) != norm(r["tt"]):
                    try:
                        changed = write_tags_safely(r["path"], r["fa"], r["ft"])
                        r["tt"] = r["ft"]
                        r["ta"] = r["fa"]
                        print(f"     ✓ 已按文件名改回：{r['fn']} -> {r['ft']}"
                              + ("" if changed else "（本来就已经对）"))
                    except Exception as exc:
                        print(f"     ✗ 改不回去：{r['fn']}: {exc}")
    return hits


def check_title_mismatch(rows, limit=15):
    """歌名与文件名不符（不算撞车），只报告 —— 可能是译名，也可能写错了。"""
    off = [r for r in rows if not r["bad"] and r["tt"].strip() and r["ft"].strip()
           and norm(r["tt"]) != norm(r["ft"])]
    print(f"\n[歌名与文件名不符] {len(off)} 首（只是提示，不改）")
    for r in off[:limit]:
        print(f"  · {r['fn']}")
        print(f"      文件里写着：{r['tt']}")
    if len(off) > limit:
        print(f"  … 另 {len(off) - limit} 首")
    return off


def check_duplicates(rows, fix, bases):
    """时长+采样率+位深+声道 相同 → 再比音频流 MD5，确认是不是同一首。"""
    buckets = {}
    for r in rows:
        if r["bad"]:
            continue
        key = stream_info(r["path"])
        if not key:
            continue
        buckets.setdefault(key, []).append(r)

    dup_groups = []
    for key, items in buckets.items():
        if len(items) < 2:
            continue
        by_md5 = {}
        for r in items:
            md5 = audio_md5(r["path"])
            if md5:
                by_md5.setdefault(md5, []).append(r)
        for md5, same in by_md5.items():
            if len(same) > 1:
                dup_groups.append(same)

    def quality(r):
        """留哪一份：先看采样率/位深，再看体积。

        2026-10-18 用户要求「以后只取音质最高的一首」，所以体积不再
        单独当标准 —— 96kHz/24bit 才是关键，体积只用来打破平手。
        """
        info = stream_info(r["path"]) or (0, 0, 0, 0)
        return (info[1], info[2], os.path.getsize(r["path"]))

    print(f"\n[重复副本] 音频完全相同：{len(dup_groups)} 组")
    for same in dup_groups:
        in_plan = [r for r in same if norm(r["stem"]) in bases]
        pool = in_plan or same
        keeper = max(pool, key=quality)
        for r in same:
            tag = "保留" if r is keeper else "多余"
            in_plan = "在收藏计划里" if norm(r["stem"]) in bases else "不在收藏计划里"
            print(f"  {'✓' if r is keeper else '✗'} {tag}  {r['fn']}"
                  f"  ({os.path.getsize(r['path'])/1048576:.1f} MB, {in_plan})")
        if fix:
            for r in same:
                if r is keeper:
                    continue
                try:
                    os.remove(r["path"])
                    print(f"     ✓ 已删除多余副本：{r['fn']}")
                except Exception as exc:
                    print(f"     ✗ 删不掉（可能正被同步/容器占用）：{r['fn']}: {exc}")
    return dup_groups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix-tags", action="store_true", help="撞名的按文件名改回")
    ap.add_argument("--fix-dup", action="store_true", help="删掉重复副本")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(DL):
        print(f"曲库目录不存在：{DL}")
        return 0

    rows = scan()
    print(f"曲库：{DL}  共 {len(rows)} 个音频文件")
    unreadable = [r for r in rows if r["bad"]]
    if unreadable:
        print(f"  读取失败：{len(unreadable)} 个")

    bases = load_plan_bases()
    hits = check_collisions(rows, args.fix_tags)
    check_title_mismatch(rows)
    dups = check_duplicates(rows, args.fix_dup, bases)

    print("\n=== 小结 ===")
    print(f"  歌名撞车 {len(hits)} 组｜重复副本 {len(dups)} 组｜读取失败 {len(unreadable)} 个")
    if hits or dups:
        print("  （本次只报告。要修复：--fix-tags / --fix-dup）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
