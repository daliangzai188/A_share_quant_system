# Read-only: what the QMT client guard sees with QMT 2.1.33 (elevated); uploads the report. Reads no credentials.
$ErrorActionPreference = 'Continue'
$home2 = Join-Path $env:LOCALAPPDATA 'A_System\qmt_client_recovery'
$out = Join-Path $env:TEMP 'guard_facts.txt'
$r = New-Object System.Collections.Generic.List[string]
$t = Get-ScheduledTask -TaskName 'A_System_QmtClientGuard' -ErrorAction SilentlyContinue
if ($t) {
  $r.Add('task state=' + $t.State + ' runlevel=' + $t.Principal.RunLevel + ' logon=' + $t.Principal.LogonType)
  $t.Actions | ForEach-Object { $r.Add('task action: ' + $_.Execute + ' ' + $_.Arguments) }
  $i = $t | Get-ScheduledTaskInfo; $r.Add('last run ' + $i.LastRunTime + ' result ' + $i.LastTaskResult)
} else { $r.Add('task A_System_QmtClientGuard: none') }
$r.Add('credentials.json present: ' + (Test-Path (Join-Path $home2 'credentials.json')))
foreach ($f in 'config.json', 'status.json', 'state.json') {
  $p = Join-Path $home2 $f
  if (Test-Path $p) { $r.Add("--- $f"); $r.Add(((Get-Content $p -Raw) -replace '\s+', ' ')) }
}
$g = Join-Path $home2 'guard.log'
if (Test-Path $g) { $r.Add('--- guard.log tail'); Get-Content $g -Tail 40 | ForEach-Object { $r.Add($_) } }
$q = Get-Process XtItClient -ErrorAction SilentlyContinue | Select-Object -First 1
$r.Add('QMT pid=' + $q.Id + ' title=' + $q.MainWindowTitle)
Get-Process python*, pythonw* -ErrorAction SilentlyContinue | ForEach-Object {
  $c = (Get-CimInstance Win32_Process -Filter ("ProcessId=" + $_.Id)).CommandLine
  $r.Add('py ' + $_.Id + ' ' + $c)
}
$r.Add('done')
$r | Set-Content -Path $out -Encoding UTF8
try {
  Invoke-WebRequest -UseBasicParsing -Method Put -InFile $out -Uri 'http://172.16.136.1:8765/upload/guard_facts.txt' -TimeoutSec 60 | Out-Null
  'uploaded guard_facts.txt'
} catch { 'UPLOAD FAILED: ' + $_.Exception.Message }
