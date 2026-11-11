# Read-only: collect the review-relevant lines (pushes, step-13 results, candidate summaries, gate
# decisions, failures) from QMT's strategy logs since a date and upload them to the Mac receiver.
# Usage: powershell -ep bypass -f review_log_lines.ps1 [yyyyMMdd]
param([string]$Since = '20260801')
$ErrorActionPreference = 'Continue'
$out = Join-Path $env:TEMP 'review_log_lines.txt'
$pat = 'A_SYSTEM_NOTIFY live|EQUITY_CURVE_STOP|REVIEW_TRIGGERS|收盘候选|组合决策明细|STATE SAVE FAILED|Traceback|收盘流水线进度：13/13|未完成|失败'
$r = New-Object System.Collections.Generic.List[string]
Get-ChildItem 'C:\QMT\GJQMT\userdata\log\XtClient_FormulaOutput_*.log' | Sort-Object Name | ForEach-Object {
  $day = $_.BaseName.Substring($_.BaseName.Length - 8)
  if ($day -lt $Since) { return }
  $r.Add('##### ' + $_.Name + ' ' + $_.Length)
  Select-String -Path $_.FullName -Pattern $pat -Encoding Default | ForEach-Object { $r.Add($_.Line) }
}
$r | Set-Content -Path $out -Encoding UTF8
Invoke-WebRequest -UseBasicParsing -Method Put -InFile $out -Uri 'http://172.16.136.1:8765/upload/review_log_lines.txt' -TimeoutSec 300 | Out-Null
'uploaded review_log_lines.txt (' + $r.Count + ' lines)'
