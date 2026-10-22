# Read-only: list A_System runtime state files (path, size, last write) and upload the list
# plus small state files (<300 KB) to the Mac receiver for the QMT-native migration.
$ErrorActionPreference = 'Continue'
$root = 'C:\A_System'
$out = Join-Path $env:TEMP 'as_state_inventory'
Remove-Item -Recurse -Force $out -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $out | Out-Null
$dirs = @('data\state', 'data\processed', 'data\live', 'reports\strategy_e', 'reports\account_risk_shadow',
          'reports\live_trade', 'reports\equity_curve_stop', 'reports\strategy_d', 'reports\execution_tracking', 'logs')
$rows = @()
foreach ($d in $dirs) {
  $p = Join-Path $root $d
  if (Test-Path $p) {
    Get-ChildItem -Path $p -File -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
      $rel = $_.FullName.Substring($root.Length + 1)
      $rows += [pscustomobject]@{ path = $rel; bytes = $_.Length; modified = $_.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss') }
    }
  }
}
Get-ChildItem -Path (Join-Path $root 'reports') -File -ErrorAction SilentlyContinue | ForEach-Object {
  $rows += [pscustomobject]@{ path = 'reports\' + $_.Name; bytes = $_.Length; modified = $_.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss') }
}
$rows | Export-Csv -NoTypeInformation -Encoding UTF8 (Join-Path $out 'inventory.csv')
$small = $rows | Where-Object { $_.bytes -lt 300000 -and ($_.path -like '*.json' -or $_.path -like '*.sqlite3' -or $_.path -like 'data\state\*' -or $_.path -like 'data\processed\*.json' -or $_.path -like 'reports\execution_tracking\*' -or $_.path -like 'reports\live_execution_audit.csv' -or $_.path -like 'reports\live_trade\combined\combined_*' -or $_.path -like 'reports\strategy_d\intraday_signals_*' -or $_.path -like 'reports\live_trade\qmt_live_order_submitted_orders.csv') }
$stage = Join-Path $out 'files'
foreach ($r in $small) {
  $dest = Join-Path $stage $r.path
  New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
  Copy-Item -LiteralPath (Join-Path $root $r.path) -Destination $dest -ErrorAction SilentlyContinue
}
$zip = Join-Path $env:TEMP 'as_state_inventory.zip'
Remove-Item -Force $zip -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $out '*') -DestinationPath $zip
$bytes = [System.IO.File]::ReadAllBytes($zip)
Invoke-WebRequest -UseBasicParsing -Method Put -Uri 'http://172.16.136.1:8765/upload/as_state_inventory.zip' -Body $bytes | Out-Null
"rows=$($rows.Count) small_files=$($small.Count) zip_bytes=$($bytes.Length) UPLOADED"
