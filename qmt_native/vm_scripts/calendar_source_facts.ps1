# Read-only: can the VM's normal Python (outside QMT) fetch the trade calendar? Never prints the token.
$ErrorActionPreference = 'Continue'
$out = Join-Path $env:TEMP 'calendar_source_facts.txt'
$r = New-Object System.Collections.Generic.List[string]
$env_file = 'C:\A_System\.env'
if (Test-Path $env_file) {
  $keys = Get-Content $env_file | Where-Object { $_ -match '^\s*[A-Za-z_]+\s*=' } | ForEach-Object { ($_ -split '=')[0].Trim() }
  $r.Add('.env keys: ' + ($keys -join ', '))
} else { $r.Add('.env missing') }
$code = @'
import sys, os
print("python", sys.version.split()[0])
try:
    import tushare as ts
    print("tushare", ts.__version__)
except Exception as e:
    print("tushare import failed:", type(e).__name__, e); raise SystemExit
token = ""
for line in open(r"C:\A_System\.env", encoding="utf-8-sig"):
    k, _, v = line.strip().partition("=")
    if k.strip() == "TUSHARE_TOKEN":
        token = v.strip().strip('"').strip("'")
print("token present:", bool(token))
if token:
    try:
        pro = ts.pro_api(token)
        df = pro.trade_cal(exchange="SSE", start_date="20261201", end_date="20271231")
        open_days = df[df["is_open"].astype(str) == "1"]["cal_date"].astype(str).sort_values()
        print("trade_cal rows", len(df), "open", len(open_days), "max", df["cal_date"].astype(str).max(),
              "first open 2027", [d for d in open_days if d >= "20270101"][:3])
        print("columns", list(df.columns))
    except Exception as e:
        print("trade_cal failed:", type(e).__name__, str(e)[:200])
'@
$tmp = Join-Path $env:TEMP 'cal_check.py'
Set-Content -Path $tmp -Value $code -Encoding UTF8
& py -3.11 $tmp 2>&1 | ForEach-Object { $r.Add([string]$_) }
Remove-Item $tmp -ErrorAction SilentlyContinue
$head = Get-Content 'C:\A_System\data\raw\trade_calendar.csv' -TotalCount 3 -ErrorAction SilentlyContinue
$r.Add('production trade_calendar.csv head: ' + ($head -join ' | '))
$r.Add('done')
$r | Set-Content -Path $out -Encoding UTF8
try {
  Invoke-WebRequest -UseBasicParsing -Method Put -InFile $out -Uri 'http://172.16.136.1:8765/upload/calendar_source_facts.txt' -TimeoutSec 60 | Out-Null
  'uploaded calendar_source_facts.txt'
} catch { 'UPLOAD FAILED: ' + $_.Exception.Message }
