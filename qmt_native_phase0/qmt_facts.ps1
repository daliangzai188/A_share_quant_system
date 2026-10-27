# Read-only facts for QMT start/stop: client path, A_System scheduled tasks, QMT login settings files.
$ErrorActionPreference = 'Continue'
"QMT process: " + ((Get-Process XtItClient -ErrorAction SilentlyContinue | Select-Object -First 1).Path)
Get-ScheduledTask | Where-Object { $_.TaskName -like 'A_System*' } | ForEach-Object { "task: $($_.TaskName) = $($_.State)" }
$guard = Join-Path $env:LOCALAPPDATA 'A_System\qmt_client_recovery\settings.json'
if (Test-Path $guard) { "guard settings: " + ((Get-Content $guard -Raw) -replace '\s+', ' ') } else { "guard settings: none" }
Get-ChildItem C:\QMT\GJQMT -Recurse -File -Include *.ini,*.xml,*.json,*.cfg,*.conf -ErrorAction SilentlyContinue |
  Where-Object { $_.Length -lt 2MB } |
  Select-String -Pattern 'autologin|auto_login|AutoLogin|autoLogin|savepass|SavePass|savePwd|rememberPass|RememberPass|remember' -List -ErrorAction SilentlyContinue |
  Select-Object -First 15 | ForEach-Object { "login cfg: " + $_.Path + " : " + $_.Line.Trim().Substring(0, [Math]::Min(100, $_.Line.Trim().Length)) }
"done"
