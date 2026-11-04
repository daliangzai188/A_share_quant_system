# Read-only: Guojin trade/quote server entries in old vs new xtuserconfig.xml + current DNS answer; uploads the report.
$ErrorActionPreference = 'Continue'
$out = Join-Path $env:TEMP 'qmt_upgrade_lines.txt'
$r = New-Object System.Collections.Generic.List[string]
foreach ($root in @('C:\QMT_backup_2.1.19', 'C:\QMT\GJQMT')) {
  $f = Join-Path $root 'config\xtuserconfig.xml'
  $r.Add("=== $f size=" + (Get-Item $f).Length)
  $lines = Get-Content $f -Encoding UTF8
  $r.Add("  BrokerBrief count=" + @($lines | Select-String '<BrokerBrief ').Count)
  $on = $false
  foreach ($l in $lines) {
    if ($l -match '<BrokerBrief ' -and $l -match '国金') { $on = $true }
    if ($on) { $t = $l.Trim(); $r.Add("  " + $t.Substring(0, [Math]::Min(200, $t.Length))) }
    if ($on -and $l -match '</BrokerBrief>') { $on = $false }
  }
  $lines | Select-String 'gjzq' | Select-Object -First 20 | ForEach-Object { $r.Add("  gjzq line " + $_.LineNumber + ": " + $_.Line.Trim()) }
}
$r.Add('=== DNS now')
try { Resolve-DnsName qmt.gjzq.com.cn -ErrorAction Stop | ForEach-Object { $r.Add("  " + $_.Name + " " + $_.Type + " " + $_.IPAddress + " " + $_.NameHost) } } catch { $r.Add("  resolve failed: " + $_.Exception.Message) }
Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object { $_.ServerAddresses } | ForEach-Object { $r.Add("  dns server " + $_.InterfaceAlias + ": " + ($_.ServerAddresses -join ',')) }
$r | Set-Content -Path $out -Encoding UTF8
try {
  Invoke-WebRequest -UseBasicParsing -Method Put -InFile $out -Uri 'http://172.16.136.1:8765/upload/qmt_upgrade_lines.txt' -TimeoutSec 60 | Out-Null
  'uploaded qmt_upgrade_lines.txt'
} catch { 'UPLOAD FAILED: ' + $_.Exception.Message }
