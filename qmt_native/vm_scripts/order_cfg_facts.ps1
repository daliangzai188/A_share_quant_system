# Read-only: context of QMT 2.1.33 config entries that may touch strategy orders; uploads the report.
$ErrorActionPreference = 'Continue'
$out = Join-Path $env:TEMP 'order_cfg_facts.txt'
$r = New-Object System.Collections.Generic.List[string]
foreach ($pair in @(@('C:\QMT\GJQMT\config\xtminiqmt.lua', 'order_check|strategy_pos_manager|ordergenerator'),
                    @('C:\QMT\GJQMT\config\xtclient.lua', 'strategy_trade_stat_panel|order_check|risk|Risk'),
                    @('C:\QMT\GJQMT\config\modules.lua', 'risk|order|strategy'))) {
  $f = $pair[0]
  $lines = Get-Content $f -Encoding UTF8
  $r.Add("=== $f")
  for ($i = 0; $i -lt $lines.Count; $i++) {
    if ($lines[$i] -match $pair[1]) {
      $lo = [Math]::Max(0, $i - 6); $hi = [Math]::Min($lines.Count - 1, $i + 3)
      for ($j = $lo; $j -le $hi; $j++) { $r.Add(('{0,5}: {1}' -f ($j + 1), $lines[$j].TrimEnd())) }
      $r.Add('  ...')
    }
  }
}
$cfg = Get-ChildItem 'C:\QMT\GJQMT\userdata\users\*\Config.xml' -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
if ($cfg -and (Test-Path $cfg)) {
  $r.Add("=== $cfg (risk / order related elements)")
  Select-String -Path $cfg -Pattern 'Risk|risk|OrderCheck|orderCheck|MaxOrder|maxOrder|PosManager' -Encoding UTF8 |
    Select-Object -First 40 | ForEach-Object { $r.Add('  ' + $_.LineNumber + ': ' + $_.Line.Trim().Substring(0, [Math]::Min(220, $_.Line.Trim().Length))) }
}
$r.Add('done')
$r | Set-Content -Path $out -Encoding UTF8
try {
  Invoke-WebRequest -UseBasicParsing -Method Put -InFile $out -Uri 'http://172.16.136.1:8765/upload/order_cfg_facts.txt' -TimeoutSec 60 | Out-Null
  'uploaded order_cfg_facts.txt'
} catch { 'UPLOAD FAILED: ' + $_.Exception.Message }
