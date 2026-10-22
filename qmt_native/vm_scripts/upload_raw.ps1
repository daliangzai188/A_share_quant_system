# Copy (read-only) the VM's own Tushare raw inputs and production selections
# from 2026-10-09 onward, zip them and upload to the Mac. Touches no source file.
$ErrorActionPreference = 'Continue'
$root = 'C:\A_System'
$stage = Join-Path $env:TEMP 'phase0_raw'
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $stage | Out-Null
$count = 0
function Copy-Rel($rel) {
  $src = Join-Path $root $rel
  if (-not (Test-Path $src)) { return }
  $dst = Join-Path $stage $rel
  New-Item -ItemType Directory (Split-Path $dst) -Force | Out-Null
  Copy-Item $src $dst -Force
  $script:count += 1
}
foreach ($sub in @('limit_list', 'daily', 'daily_basic', 'adj_factor')) {
  $dir = Join-Path $root ('data\raw\' + $sub)
  if (Test-Path $dir) {
    Get-ChildItem $dir -Filter '2026*.csv' | Where-Object { $_.BaseName -ge '20261009' } |
      ForEach-Object { Copy-Rel ('data\raw\' + $sub + '\' + $_.Name) }
  }
}
foreach ($rel in @('data\raw\trade_calendar.csv', 'data\raw\stock_basic\stock_basic_all.csv',
                   'data\processed\stock_theme_reference.csv')) { Copy-Rel $rel }
$ops = Join-Path $root 'reports\paper_trade\ab_filtered_daily_ops'
if (Test-Path $ops) {
  Get-ChildItem $ops -Filter 'a_strict_plus_c_hold3_202610*' |
    Where-Object { $_.Name.Substring(22, 8) -ge '20261009' } |
    ForEach-Object { Copy-Rel ('reports\paper_trade\ab_filtered_daily_ops\' + $_.Name) }
}
$ed = Join-Path $root 'reports\strategy_e'
if (Test-Path $ed) {
  Get-ChildItem $ed -Filter 'e_signal_202610*' | ForEach-Object { Copy-Rel ('reports\strategy_e\' + $_.Name) }
}
Write-Host ('files staged: ' + $count)
$zip = Join-Path $env:TEMP 'phase0_raw.zip'
Compress-Archive -Path ($stage + '\*') -DestinationPath $zip -Force
try {
  Invoke-WebRequest -UseBasicParsing -Method Put -InFile $zip -Uri 'http://172.16.136.1:8765/upload/phase0_raw.zip' -TimeoutSec 900 | Out-Null
  Write-Host 'RAW UPLOADED'
} catch { Write-Host ('UPLOAD FAILED: ' + $_.Exception.Message) }
