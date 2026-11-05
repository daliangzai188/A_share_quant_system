# Restart the notification forwarder task and upload its hash, state and recent log.
$out = Join-Path $env:TEMP 'restart_forwarder.txt'
$r = @('hash ' + (Get-FileHash C:\A_System\qmt_native_phase0\qmt_notify_forwarder.py).Hash.Substring(0,16))
Stop-ScheduledTask A_System_QmtNotifyForwarder; Start-Sleep 2; Start-ScheduledTask A_System_QmtNotifyForwarder; Start-Sleep 40
$r += 'task: ' + (Get-ScheduledTask A_System_QmtNotifyForwarder).State
$r += 'tasklist: ' + ((tasklist /FI "IMAGENAME eq XtItClient.exe" /FO CSV /NH) -join ' ')
$r += Get-Content C:\A_System\logs\qmt_notify_forwarder.log -Tail 6
$r | Set-Content -Encoding UTF8 $out
Invoke-WebRequest -UseBasicParsing -Method Put -InFile $out -Uri 'http://172.16.136.1:8765/upload/restart_forwarder.txt' | Out-Null
'uploaded restart_forwarder.txt'
