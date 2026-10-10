$L='C:\QMT\GJQMT\userdata\log'
"### 10-09 log tail (the moment it auto-logged-out) ###"
Get-Content "$L\XtClient_20261009.log" -Tail 120 | Where-Object { $_ -notmatch 'is alive|not logged' } | Select-Object -Last 45
""
"### 10-10 log head (what it did after) ###"
Get-Content "$L\XtClient_20261010.log" | Where-Object { $_ -notmatch 'is alive|not logged' } | Select-Object -First 45
""
"### Message log ###"
Get-Content "$L\XtClient_Message_20261010.log" -Tail 25 -ErrorAction SilentlyContinue
""
"### guest time / tz ###"
(Get-Date).ToString('s'); (Get-TimeZone).Id
""
"### guest egress check ###"
try { (Invoke-RestMethod 'https://ipinfo.io/json' -TimeoutSec 8) | Select-Object ip,country,org | Format-List } catch { "egress check failed: $($_.Exception.Message)" }
