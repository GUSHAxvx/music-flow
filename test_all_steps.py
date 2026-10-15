#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全流程逐步骤测试器

用途：交付前确认每个步骤都能独立跑通。
做法：依次通过 API 触发每一步（观察模式），轮询到结束，记录成功/失败与耗时。
"""
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8477"

STEPS = [
    ("docker",     "① 启动 Docker",      120),
    ("containers", "② 启动容器",          90),
    ("syncthing",  "③ 启动 Syncthing",    90),
    ("download",   "④ 检查并下载新收藏", 900),
    ("decode",     "⑤ 解码加密音乐",     900),
    ("maintain",   "⑥ 落盘与整理",       900),
    ("covers",     "⑦ 封面与歌词",       900),
    ("coverback",  "⑧ 回写暂存",         300),
    ("verify",     "⑨ 安全检查",         300),
    ("integrity",  "⑩ 曲库体检",         300),
    ("sync",       "⑪ 同步到手机",       300),
]

ONLY = sys.argv[1:] if len(sys.argv) > 1 else None


def api(path, method="GET", body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def wait_idle(max_wait):
    """等到 runner 不再运行"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        st = api("/api/status", timeout=40)
        r = st.get("runner", {})
        if not r.get("running"):
            return r, time.time() - t0
        time.sleep(4)
    return api("/api/status", timeout=40).get("runner", {}), time.time() - t0


def tail_log(n=14):
    runs = api("/api/runs").get("runs", [])
    if not runs:
        return []
    d = api("/api/run?name=" + urllib.parse.quote(runs[0]["name"]))
    lines = [l for l in (d.get("content") or "").splitlines() if l.strip()]
    return lines[-n:]


print("=" * 74)
print("Music Flow 全流程测试")
print("=" * 74)

# 等服务起来
for _ in range(20):
    try:
        api("/api/pipeline", timeout=5)
        break
    except Exception:
        time.sleep(1)
else:
    sys.exit("服务没起来（http://127.0.0.1:8477）")

results = []
for sid, name, tmo in STEPS:
    if ONLY and sid not in ONLY:
        continue
    print()
    print(f"───── 测试 {name} ({sid}) ─────")
    try:
        api("/api/run", "POST", {"mode": "manual", "step": sid})
    except Exception as exc:
        print(f"  触发失败: {exc}")
        results.append((sid, name, "触发失败", 0))
        continue

    t0 = time.time()
    runner, _ = wait_idle(tmo)
    dur = time.time() - t0
    res = runner.get("results", {}).get(sid, "?")
    mark = "✅" if res == "ok" else "❌"
    print(f"  {mark} 结果={res}  耗时={dur:.0f}s")
    for line in tail_log(10):
        print(f"     {line}")
    results.append((sid, name, res, dur))

print()
print("=" * 74)
print("测试汇总")
print("=" * 74)
ok = sum(1 for r in results if r[2] == "ok")
for sid, name, res, dur in results:
    mark = "✅" if res == "ok" else "❌"
    print(f"  {mark} {name:<22} {res:<6} {dur:>6.0f}s")
print()
print(f"  通过 {ok} / {len(results)}")
