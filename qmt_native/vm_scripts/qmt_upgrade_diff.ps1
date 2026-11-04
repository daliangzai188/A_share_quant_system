# Read-only: line diffs of QMT config files changed by the 2.1.19 -> 2.1.33 upgrade; uploads the report.
$ErrorActionPreference = 'Continue'
$new = 'C:\QMT\GJQMT\config'
$old = 'C:\QMT_backup_2.1.19\config'
$out = Join-Path $env:TEMP 'qmt_upgrade_diff.txt'
$r = New-Object System.Collections.Generic.List[string]
foreach ($name in @('xtuserconfig.xml', 'xtclient.lua', 'clientEnv.lua', 'modules.lua', 'xtminiqmt.lua', 'helperContent.ini', 'fun.xml')) {
  $a = Join-Path $old $name; $b = Join-Path $new $name
  if (-not ((Test-Path $a) -and (Test-Path $b))) { $r.Add("=== $name missing"); continue }
  $d = Compare-Object (Get-Content $a -Encoding UTF8) (Get-Content $b -Encoding UTF8)
  $r.Add("=== $name : " + @($d).Count + " changed lines (<= old, => new)")
  $d | Select-Object -First 60 | ForEach-Object {
    $t = $_.InputObject.Trim()
    $r.Add("  " + $_.SideIndicator + " " + $t.Substring(0, [Math]::Min(220, $t.Length)))
  }
}
$r | Set-Content -Path $out -Encoding UTF8
try {
  Invoke-WebRequest -UseBasicParsing -Method Put -InFile $out -Uri 'http://172.16.136.1:8765/upload/qmt_upgrade_diff.txt' -TimeoutSec 60 | Out-Null
  'uploaded qmt_upgrade_diff.txt'
} catch { 'UPLOAD FAILED: ' + $_.Exception.Message }
