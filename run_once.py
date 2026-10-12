#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑一次完整流水线并打印结果摘要（测试用）"""
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8477"
STEPS = ["docker", "containers", "syncthing", "download",
         "maintain", "verify", "integrity", "sync"]
KEYS = ("下调", "上调", "最终曲库", "落地", "精简删除", "同步完成",
        "同步进度", "安全中止", "待下 无损", "下载完成")


def api(path, method="GET", body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    print("=== 全流程 ===" if not only else f"=== 只跑 {only} ===", flush=True)
    api("/api/run", "POST",
        {"mode": "auto"} if not only else {"mode": "manual", "step": only})

    t0, last = time.time(), ""
    while time.time() - t0 < 2400:
        r = api("/api/status")["runner"]
        cur = r.get("current") or "-"
        if cur != last:
            print("  [%5.0fs] %s" % (time.time() - t0, cur), flush=True)
            last = cur
        if not r["running"]:
            break
        time.sleep(8)

    r = api("/api/status")["runner"]
    print(flush=True)
    for k in STEPS:
        v = r["results"].get(k, "-")
        print("  %-5s %s" % (v, k), flush=True)

    runs = api("/api/runs")["runs"]
    d = api("/api/run?name=" + urllib.parse.quote(runs[0]["name"]))
    print(flush=True)
    seen = set()
    for line in (d.get("content") or "").splitlines():
        if any(k in line for k in KEYS) and line not in seen:
            seen.add(line)
            print("  " + line[:125], flush=True)


if __name__ == "__main__":
    main()
