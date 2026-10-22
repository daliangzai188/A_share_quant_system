# Read-only: upload production outputs of one signal date plus the QMT shadow export
# (C:\qn_shadow_out) to the Mac receiver for side-by-side comparison.
# Usage: powershell -ep bypass -f upload_compare.ps1 20261021
param([string]$Day = (Get-Date -Format 'yyyyMMdd'))
$ErrorActionPreference = 'Continue'
$root = 'C:\A_System'
$stage = Join-Path $env:TEMP ('as_compare_' + $Day)
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path (Join-Path $stage 'production') | Out-Null
$patterns = @("data\raw\limit_list\$Day.csv", "data\raw\daily\$Day.csv", "data\raw\daily_basic\$Day.csv",
              'data\processed\live_*.csv', 'data\state\equity_curve_stop_decision.json',
              'reports\equity_curve_stop\shadow_nav_latest.csv',
              "reports\paper_trade\ab_filtered_daily_ops\*_$Day*", "reports\strategy_e\e_signal_$Day*",
              'reports\strategy_e\*.json', "reports\live_trade\combined\*_$Day*")
foreach ($p in $patterns) {
  Get-ChildItem -Path (Join-Path $root $p) -File -ErrorAction SilentlyContinue | ForEach-Object {
    $rel = $_.FullName.Substring($root.Length + 1)
    $dest = Join-Path (Join-Path $stage 'production') $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $dest
  }
}
if (Test-Path 'C:\qn_shadow_out') { Copy-Item -Recurse 'C:\qn_shadow_out' (Join-Path $stage 'shadow') }
$zip = Join-Path $env:TEMP ('as_compare_' + $Day + '.zip')
Remove-Item -Force $zip -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip
$bytes = [System.IO.File]::ReadAllBytes($zip)
Invoke-WebRequest -UseBasicParsing -Method Put -Uri ('http://172.16.136.1:8765/upload/as_compare_' + $Day + '.zip') -Body $bytes | Out-Null
"compare package for $Day uploaded: $($bytes.Length) bytes"
