# -*- coding: utf-8 -*-
"""检查所有 .ps1 的「编码地雷」，防止再犯 2026-10-15 那次事故

背景（真实事故，不是假想）
--------------------------
`scripts/pick_files.ps1` 里全是中文字符串（对话框标题、文件过滤器、错误提示）。
它本来是 **UTF-8 带 BOM** 的，所以一直正常。2026-10-15 改这个文件时用文本
编辑器写回，**把 BOM 吃掉了**，于是：

    PowerShell 5.1 按系统 ANSI(CP936) 解读 UTF-8 字节
      -> 中文变乱码，其中某些字节恰好是 0x27（单引号）
      -> 引号提前闭合，解析器报「字符串缺少终止符」
      -> **整个脚本解析失败，run 都不 run**
      -> 用户点「导入歌曲」什么都不弹，日志只写「未选择任何内容」

排查时最难的地方是：脚本以 exit 0 静默失败，调用方拿不到任何错误。

这个检查要拦住的正是它
----------------------
规则很简单，两条：

  R1  含非 ASCII 字符的 .ps1        -> 必须有 UTF-8 BOM
  R2  没有 BOM 的 .ps1              -> 必须是纯 ASCII

违反任一条，Windows PowerShell 5.1 都会读错。纯 ASCII + 无 BOM 是安全的，
所以 `pixel_stats.ps1` 故意保持 ASCII 不会被误报。

用法
----
    python scripts/check_ps1_encoding.py            # 检查（有问题 exit 1）
    python scripts/check_ps1_encoding.py --fix      # 顺手补 BOM

改完 .ps1 之后跑一下，或者放进提交前的自检里。
"""
import os
import sys

BOM = b"\xef\xbb\xbf"

# 扫描范围：本仓库 + 旁边那个容器用的目录（samplerate 闸门等脚本在那儿）
ROOTS = [
    r"D:\music-flow",
    r"D:\music-monitor",
]

SKIP_DIRS = {".git", "__pycache__", "_import_tmp", "node_modules", ".venv", "venv"}


def scan():
    """返回 (待补 BOM 的文件列表, 真正坏掉的文件列表, 统计)"""
    need_bom, broken, stats = [], [], {"total": 0, "ascii": 0, "ok": 0}

    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if not fn.lower().endswith(".ps1"):
                    continue
                path = os.path.join(dirpath, fn)
                stats["total"] += 1
                try:
                    raw = open(path, "rb").read()
                except OSError:
                    continue

                has_bom = raw.startswith(BOM)
                body = raw[len(BOM):] if has_bom else raw

                # 正文本身能不能按 UTF-8 解出来？解不出说明已经被写坏了
                try:
                    body.decode("utf-8")
                    body_ok = True
                except UnicodeDecodeError:
                    body_ok = False

                non_ascii = any(b > 127 for b in body)

                if not body_ok:
                    broken.append((path, "正文不是合法 UTF-8（可能已被写坏）"))
                elif non_ascii and not has_bom:
                    need_bom.append(path)
                elif not non_ascii:
                    stats["ascii"] += 1
                else:
                    stats["ok"] += 1

    return need_bom, broken, stats


def add_bom(path):
    """给文件补 UTF-8 BOM（正文必须先是合法 UTF-8，否则拒绝动它）"""
    raw = open(path, "rb").read()
    if raw.startswith(BOM):
        return False
    raw.decode("utf-8")          # 不合法就抛异常，宁可不改也不写坏
    with open(path, "wb") as fh:
        fh.write(BOM + raw)
    return True


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    do_fix = "--fix" in sys.argv

    need_bom, broken, stats = scan()

    print(f"扫描 .ps1：共 {stats['total']} 个"
          f"（纯 ASCII 免检 {stats['ascii']} 个，合规 {stats['ok']} 个）")
    print()

    if not need_bom and not broken:
        print("✅ 全部合规：含中文的 .ps1 都带 BOM，没 BOM 的都是纯 ASCII")
        return 0

    if need_bom:
        print(f"⚠ 含非 ASCII 但没有 BOM：{len(need_bom)} 个")
        print("  （PowerShell 5.1 会按 ANSI 读，中文变乱码 -> 解析失败 -> 静默不工作）")
        for path in need_bom:
            if do_fix:
                try:
                    if add_bom(path):
                        print(f"    ✓ 已补 BOM  {path}")
                    else:
                        print(f"    - 已有 BOM  {path}")
                except UnicodeDecodeError:
                    print(f"    ✗ 正文不是合法 UTF-8，拒绝改写  {path}")
            else:
                print(f"    · {path}")
        print()

    if broken:
        print(f"✗ 文件已损坏：{len(broken)} 个")
        for path, why in broken:
            print(f"    · {path}  —— {why}")
        print()

    if do_fix and need_bom:
        # 补完再验一遍
        still, broken2, _ = scan()
        if not still and not broken2:
            print("✅ 已全部修复")
            return 0
        print(f"修复后仍有 {len(still)} 个待处理")
        return 1

    if need_bom:
        print("修复方式：python scripts/check_ps1_encoding.py --fix")
    return 1


if __name__ == "__main__":
    sys.exit(main())
