# ⚠⚠ 本文件必须保存为 UTF-8 **带 BOM**（BOM = 开头那三个字节 EF BB BF）⚠⚠
# ---------------------------------------------------------------------------
# 不要用会吃掉 BOM 的编辑器/工具直接覆盖这个文件。理由是真的发生过事故：
#
#   PowerShell 5.1 读 .ps1 的规则是「有 BOM 按 BOM，没 BOM 按系统 ANSI(CP936)」。
#   这个文件里有大量中文字符串，一旦丢了 BOM，中文被按 GBK 解读成乱码字节，
#   其中某些字节恰好是 0x27（单引号）→ 引号提前闭合 →
#   解析器报「字符串缺少终止符」→ **整个脚本解析失败，run 都不 run**。
#   2026-10-15 22:12 用户报「点了不弹选文件的界面」就是这个：
#   脚本 exit 0 静默失败，调用方只看到「未选择任何内容」，极难排查。
#
# 改完请跑一次自检（会告诉你 BOM 有没有丢，并能自动补回）：
#     python scripts/check_ps1_encoding.py
#     python scripts/check_ps1_encoding.py --fix
# ---------------------------------------------------------------------------

# 用 Windows 原生对话框选文件 / 选文件夹，把结果路径输出到 stdout。
#
# 为什么用 PowerShell 而不是浏览器的 <input type="file">：
# 浏览器出于安全**不会**给出文件的真实磁盘路径，只能上传内容副本。
# 而导入流程需要真实路径（要拷贝、要交给 Syncthing 的目录），
# 所以走系统对话框最直接，也不用把几百 MB 的文件塞进 HTTP 请求体。
#
# 用法：
#   powershell -File pick_files.ps1 -Mode files     选一个或多个音频文件
#   powershell -File pick_files.ps1 -Mode folder    选一个文件夹
param(
    [ValidateSet('files', 'folder')]
    [string]$Mode = 'files'
)

# 输出编码必须显式指定（踩过的坑）
# ------------------------------------
# PowerShell 5.1 往管道写字符串时用的是控制台输出编码 —— 中文系统上是
# OEM 代码页 936(GBK)，**不是** UTF-8。而调用方（import_songs.py）之前
# 按 UTF-8 读，于是路径里每个中文字符都变成 U+FFFD：
#
#     界面选中  E:\音乐\KugouMusic\李润祺 - 茫.kgg
#     读出      E:\????\KugouMusic\?????? - ?.kgg   -> 文件不存在 -> 导入失败
#
# 纯英文路径两种编码结果一样，所以这个 bug 只在中文名上暴露出来。
# 这里把输出编码钉死成 UTF-8（不带 BOM），调用方再按 UTF-8 解码就不会错。
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false

Add-Type -AssemblyName System.Windows.Forms | Out-Null

if ($Mode -eq 'folder') {
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = '选择要导入的文件夹（其中的音频文件会全部导入）'
    $dlg.ShowNewFolderButton = $false
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        Write-Output $dlg.SelectedPath
    }
} else {
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Title = '选择要导入的歌曲（可多选）'
    $dlg.Filter = '音频文件|*.flac;*.mp3;*.m4a;*.ogg;*.wav;*.ape;*.wma|所有文件|*.*'
    $dlg.Multiselect = $true
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        foreach ($f in $dlg.FileNames) { Write-Output $f }
    }
}
