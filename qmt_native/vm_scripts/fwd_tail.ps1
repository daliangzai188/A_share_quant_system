# Read-only: upload the notification forwarder's recent log and task state.
$out = Join-Path $env:TEMP 'fwd_tail_now.txt'
$r = @(Get-Content C:\A_System\logs\qmt_notify_forwarder.log -Tail 40)
$r += 'task: ' + (Get-ScheduledTask A_System_QmtNotifyForwarder).State
$r | Set-Content -Encoding UTF8 $out
Invoke-WebRequest -UseBasicParsing -Method Put -InFile $out -Uri 'http://172.16.136.1:8765/upload/fwd_tail_now.txt' | Out-Null
'uploaded fwd_tail_now.txt'
