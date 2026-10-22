# Install (or update) the Windows scheduled task that keeps qmt_notify_forwarder.py running:
# starts at user logon, restarts one minute after a crash, one instance only, no console window.
# The forwarder runs outside QMT and has no trading controls (it only reads QMT's log).
# Usage: powershell -ep bypass -f install_notify_forwarder.ps1
$ErrorActionPreference = 'Stop'
$name = 'A_System_QmtNotifyForwarder'
$script = 'C:\A_System\qmt_native_phase0\qmt_notify_forwarder.py'
$py = (& py -3.11 -c "import sys; print(sys.executable)").Trim()
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { throw "pythonw.exe not found next to $py" }
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pyw -Argument ('"' + $script + '"') -WorkingDirectory 'C:\A_System'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
  -Description 'A_System: forward phone notifications from the QMT log to Bark (outside QMT, no trading controls)' `
  -Force | Out-Null
Start-ScheduledTask -TaskName $name
Start-Sleep -Seconds 5
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State | Format-Table -AutoSize
Get-Content 'C:\A_System\logs\qmt_notify_forwarder.log' -Tail 3 -ErrorAction SilentlyContinue
