# Read-only: send the BARK_URL line of C:\A_System\.env to the Mac receiver, so the Mac's monthly
# maintenance / plan-jia review reminder (tools/install_mac_maintenance_reminder.py) can be
# installed. The Mac's synced folder has no .env; the line is never printed here.
$ErrorActionPreference = 'Stop'
$line = Select-String -Path 'C:\A_System\.env' -SimpleMatch 'BARK_URL=' | Select-Object -First 1 -ExpandProperty Line
if (-not $line) { 'BARK_URL not found in C:\A_System\.env'; exit 1 }
$tmp = Join-Path $env:TEMP 'bark_line.txt'
try {
  Set-Content -Path $tmp -Value $line.Trim() -Encoding ascii
  Invoke-WebRequest -UseBasicParsing -Method Put -InFile $tmp -Uri 'http://172.16.136.1:8765/upload/bark_line.txt' -TimeoutSec 60 | Out-Null
  'uploaded bark_line.txt'
} finally {
  Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}
