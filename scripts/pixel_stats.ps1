# Decode one image with .NET System.Drawing and print "mean,stddev".
# Used by fix_covers.py to detect solid-color placeholder covers
# (the target environment has no PIL).
# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads .ps1 files
# without a BOM as ANSI, so non-ASCII source text gets mangled and the
# script fails to parse.
param(
    [Parameter(Mandatory = $true)][string]$Path
)

Add-Type -AssemblyName System.Drawing

try {
    $img = [System.Drawing.Image]::FromFile($Path)
} catch {
    exit 1
}

try {
    $bmp = New-Object System.Drawing.Bitmap $img
    $stepX = [Math]::Max(1, [int]($bmp.Width / 40))
    $stepY = [Math]::Max(1, [int]($bmp.Height / 40))
    $sum = 0.0
    $sum2 = 0.0
    $cnt = 0
    for ($y = 0; $y -lt $bmp.Height; $y += $stepY) {
        for ($x = 0; $x -lt $bmp.Width; $x += $stepX) {
            $c = $bmp.GetPixel($x, $y)
            # Rec.601 luma weights
            $l = 0.299 * $c.R + 0.587 * $c.G + 0.114 * $c.B
            $sum += $l
            $sum2 += $l * $l
            $cnt++
        }
    }
    $bmp.Dispose()
    if ($cnt -eq 0) { exit 1 }
    $mean = $sum / $cnt
    $var = [Math]::Max(0.0, $sum2 / $cnt - $mean * $mean)
    $std = [Math]::Sqrt($var)
    Write-Output ("{0:N4},{1:N4}" -f $mean, $std)
} finally {
    $img.Dispose()
}
