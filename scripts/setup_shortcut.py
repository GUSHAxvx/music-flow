#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成启动器与桌面快捷方式。

关键点（都是踩过的坑）：
* .bat 必须 **ANSI/GBK 编码 + CRLF 换行**。
  UTF-8 会让 cmd 把中文当命令解析，报「'xx' 不是内部或外部命令」；
  纯 LF 换行在部分 Windows 上也会解析异常。
* 快捷方式图标用项目自带的 assets/music-flow.ico。
"""
import os
import subprocess
import sys

BASE = r"D:\music-flow"
BAT = os.path.join(BASE, "启动音乐管家.bat")
ICO = os.path.join(BASE, "assets", "music-flow.ico")

BAT_CONTENT = """@echo off
title Music Flow - 音乐管家
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [X] 没有找到 Python，请先安装 Python 3.10+ 并加入 PATH
  echo.
  pause
  exit /b 1
)

echo 正在启动 Music Flow ...
echo 浏览器会自动打开；这个窗口保持开着，关掉窗口即退出程序。
echo.
python music_flow.py
if errorlevel 1 (
  echo.
  echo [!] 启动失败，请看上面的错误信息。
  echo.
  pause
)
"""


def write_bat():
    # GBK(ANSI) + CRLF —— 绝对不能是 UTF-8
    data = BAT_CONTENT.replace("\n", "\r\n")
    with open(BAT, "wb") as fh:
        fh.write(data.encode("gbk", errors="replace"))
    size = os.path.getsize(BAT)
    raw = open(BAT, "rb").read()
    crlf = raw.count(b"\r\n")
    print(f"已写入启动器: {BAT}")
    print(f"  编码: GBK/ANSI（无 BOM，前 3 字节 {raw[0]},{raw[1]},{raw[2]}）")
    print(f"  换行: CRLF × {crlf}")
    print(f"  大小: {size} 字节")


def make_shortcut(desktop):
    ps = f"""
$W = New-Object -ComObject WScript.Shell
$lnk = $W.CreateShortcut('{desktop}\\Music Flow.lnk')
$lnk.TargetPath = '{BAT}'
$lnk.WorkingDirectory = '{BASE}'
$lnk.IconLocation = '{ICO},0'
$lnk.Description = 'Music Flow - 音乐下载/监测/同步管家'
$lnk.WindowStyle = 1
$lnk.Save()
Write-Output 'created'
"""
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    lnk = os.path.join(desktop, "Music Flow.lnk")
    ok = os.path.exists(lnk)
    print(f"桌面快捷方式: {lnk}")
    print(f"  创建结果: {'✅ 成功' if ok else '❌ 失败'}")
    if err:
        print(f"  错误: {err[:200]}")
    if ok:
        print(f"  大小: {os.path.getsize(lnk)} 字节")
    return ok


def main():
    if not os.path.exists(ICO):
        print(f"❌ 图标不存在: {ICO}")
        return 1
    write_bat()
    print()
    desktop = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace").stdout.strip()
    if not desktop or not os.path.isdir(desktop):
        print(f"❌ 找不到桌面目录: {desktop!r}")
        return 1
    print(f"桌面目录: {desktop}")
    print()
    return 0 if make_shortcut(desktop) else 1


if __name__ == "__main__":
    sys.exit(main())
