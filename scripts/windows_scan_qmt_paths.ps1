param(
    [string]$ProjectRoot = "Z:\"
)

$ErrorActionPreference = "SilentlyContinue"

$ProjectRoot = $ProjectRoot.Trim('"')
if ($ProjectRoot.EndsWith("\")) {
    $ProjectRoot = $ProjectRoot.TrimEnd("\")
}

$outputDir = Join-Path $ProjectRoot "reports\live_trade"
$outputPath = Join-Path $outputDir "qmt_path_candidates.txt"
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

$rows = New-Object System.Collections.Generic.List[string]
$rows.Add("QMT path scan")
$rows.Add("Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$rows.Add("")

$shell = New-Object -ComObject WScript.Shell
$shortcutRoots = @(
    [Environment]::GetFolderPath("Desktop"),
    [Environment]::GetFolderPath("CommonDesktopDirectory"),
    [Environment]::GetFolderPath("Programs"),
    [Environment]::GetFolderPath("CommonPrograms")
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

$rows.Add("[QMT shortcuts]")
foreach ($root in $shortcutRoots) {
    Get-ChildItem -Path $root -Filter "*.lnk" -Recurse |
        Where-Object { $_.Name -match "QMT|国金|迅投|智能策略" } |
        ForEach-Object {
            $shortcut = $shell.CreateShortcut($_.FullName)
            $rows.Add("shortcut=$($_.FullName)")
            $rows.Add("target=$($shortcut.TargetPath)")
            $rows.Add("working_directory=$($shortcut.WorkingDirectory)")
            $rows.Add("")
        }
}

$driveRoots = Get-PSDrive -PSProvider FileSystem |
    Where-Object { $_.Name -ne "Z" -and (Test-Path "$($_.Name):\") } |
    ForEach-Object { "$($_.Name):\" }

$rows.Add("[Candidate directories]")
$namePatterns = @(
    "userdata_mini",
    "userdata",
    "xtquant",
    "XtQuant",
    "QMT",
    "*QMT*",
    "*国金*",
    "*迅投*"
)

foreach ($root in $driveRoots) {
    $rows.Add("search_root=$root")
    foreach ($pattern in $namePatterns) {
        Get-ChildItem -Path $root -Directory -Filter $pattern -Recurse |
            Select-Object -First 30 |
            ForEach-Object { $rows.Add($_.FullName) }
    }
}

$rows.Add("")
$rows.Add("[Python xtquant files]")
foreach ($root in $driveRoots) {
    Get-ChildItem -Path $root -File -Include "xttrader.py","xtdata.py","xtconstant.py","xttype.py","xtquant*.pyd","xtquant*.py","*xtquant*.whl","*xtquant*.zip" -Recurse |
        Select-Object -First 30 |
        ForEach-Object { $rows.Add($_.FullName) }
}

$rows.Add("")
$rows.Add("[Python xtquant directories]")
foreach ($root in $driveRoots) {
    Get-ChildItem -Path $root -Directory -Filter "xtquant" -Recurse |
        Select-Object -First 30 |
        ForEach-Object { $rows.Add($_.FullName) }
}

$rows | Set-Content -Path $outputPath -Encoding UTF8
$rows | Select-Object -First 120 | ForEach-Object { Write-Host $_ }
Write-Host ""
Write-Host "Full scan report: $outputPath"
