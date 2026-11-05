# Read-only: facts for QMT 2.1.33's network monitor false stops; uploads the report. Changes nothing.
$ErrorActionPreference = 'Continue'
$out = Join-Path $env:TEMP 'monitor_facts.txt'
$r = New-Object System.Collections.Generic.List[string]
foreach ($store in 'Cert:\LocalMachine\Root', 'Cert:\CurrentUser\Root', 'Cert:\LocalMachine\CA', 'Cert:\CurrentUser\CA') {
  Get-ChildItem $store -ErrorAction SilentlyContinue | Where-Object { $_.Subject -match 'GlobalSign|GTS|Google' } |
    ForEach-Object { $r.Add("$store | " + $_.Subject + ' | ' + $_.Thumbprint + ' | until ' + $_.NotAfter.ToString('yyyy-MM-dd')) }
}
$r.Add('--- cached certificate/CRL downloads (CryptnetUrlCache)')
certutil -urlcache * | Select-String -Pattern '^\s*(http|ldap)' | ForEach-Object { $r.Add('  ' + $_.Line.Trim()) }
$r.Add('--- QMT monitor lines today')
foreach ($day in (Get-Date).AddDays(-1).ToString('yyyyMMdd'), (Get-Date).ToString('yyyyMMdd')) {
  $p = "C:\QMT\GJQMT\userdata\log\XtClient_$day.log"
  if (-not (Test-Path $p)) { continue }
  $fs = [IO.File]::Open($p, 'Open', 'Read', 'ReadWrite'); $sr = New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)
  $n = 0
  while ($null -ne ($line = $sr.ReadLine())) {
    if ($line -match '\[monitor\](TCP| illegal)|TradeStrategy\]try (stop|run)|AutoRunStrategy|illegal|Illegal') {
      $n++; if ($n -le 400) { $r.Add('  ' + $line.Substring(0, [Math]::Min(230, $line.Length))) }
    }
  }
  $sr.Close(); $fs.Close(); $r.Add("  (${day}: $n matching lines)")
}
$r.Add('done')
$r | Set-Content -Path $out -Encoding UTF8
try {
  Invoke-WebRequest -UseBasicParsing -Method Put -InFile $out -Uri 'http://172.16.136.1:8765/upload/monitor_facts.txt' -TimeoutSec 60 | Out-Null
  'uploaded monitor_facts.txt'
} catch { 'UPLOAD FAILED: ' + $_.Exception.Message }
