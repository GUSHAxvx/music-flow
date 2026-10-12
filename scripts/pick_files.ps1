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
