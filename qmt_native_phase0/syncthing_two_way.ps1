# Switch the A_System Syncthing folder on this VM to two-way sync (2026-11-14, user's request:
# "两边的数据都要互相实时同步，只要在A_System文件夹下的").
#
# Default (no switch): READ ONLY. Prints the folder type, ignore lines, versioning and every item this
# VM changed locally (receive-only), i.e. exactly what will flow to the Mac once both sides send.
# -Apply: 1) adds the ignore lines .git and *.tmp (the Mac's git repository must never come back;
#            half-written temp files are not synced);
#         2) keeps replaced or deleted files for 30 days outside the folder, in C:\A_System_sync_versions;
#         3) sets the folder type to Send & Receive.
# The Mac side is switched separately (its .stignore is in git; type/versioning via the Mac's own API).
# Results: %TEMP%\syncthing_two_way.txt, uploaded to the Mac receiver (172.16.136.1:8765) when it runs.
#
# Usage: powershell -ExecutionPolicy Bypass -File C:\A_System\qmt_native_phase0\syncthing_two_way.ps1 [-Apply]
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$FolderId = 'a-system'
$Api = 'http://127.0.0.1:8384'
$Versions = 'C:\A_System_sync_versions'

$cfg = @("$env:LOCALAPPDATA\Syncthing\config.xml", "$env:APPDATA\Syncthing\config.xml",
         "$env:USERPROFILE\AppData\Local\Syncthing\config.xml") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $cfg) { throw 'Syncthing config.xml not found' }
[xml]$xml = Get-Content -Raw -Encoding UTF8 $cfg
$Headers = @{ 'X-API-Key' = [string]$xml.configuration.gui.apikey }
$Out = New-Object System.Collections.Generic.List[string]

function Get-Api([string]$Path) { Invoke-RestMethod -Uri ($Api + $Path) -Headers $Headers -Method Get -TimeoutSec 120 }
function Send-Api([string]$Path, [string]$Method, $Object) {
  $json = ConvertTo-Json -InputObject $Object -Depth 6
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
  Invoke-RestMethod -Uri ($Api + $Path) -Headers $Headers -Method $Method -ContentType 'application/json; charset=utf-8' -Body $bytes -TimeoutSec 120 | Out-Null
}
function Say([string]$Text) { $Out.Add($Text); Write-Host $Text }

function Show-Facts([string]$Label) {
  $f = Get-Api "/rest/config/folders/$FolderId"
  Say ('== ' + $Label + '  ' + (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
  Say ('folder type: ' + $f.type + '  path: ' + $f.path + '  versioning: ' + $f.versioning.type + ' ' + $f.versioning.fsPath)
  $ign = Get-Api "/rest/db/ignores?folder=$FolderId"
  Say ('ignore lines: ' + ((@($ign.ignore) | Where-Object { $_ -and -not $_.StartsWith('//') }) -join ' | '))
  $st = Get-Api "/rest/db/status?folder=$FolderId"
  Say ('state: ' + $st.state + '  localFiles: ' + $st.localFiles + '  needFiles: ' + $st.needFiles +
       '  locallyChangedFiles: ' + $st.receiveOnlyChangedFiles + '  locallyChangedBytes: ' + $st.receiveOnlyChangedBytes)
  $all = @()
  $page = 1
  do {
    $lc = Get-Api "/rest/db/localchanged?folder=$FolderId&page=$page&perpage=1000"
    $batch = @($lc.files)
    $all += $batch
    $page += 1
  } while ($batch.Count -eq 1000)
  Say ('locally changed items (will go to the Mac after the switch): ' + $all.Count)
  $all | Group-Object { (($_.name -split '[\\/]')[0]) + $(if ($_.deleted) { ' (deleted here)' } else { '' }) } |
    Sort-Object Count -Descending | ForEach-Object {
      $mb = [math]::Round((($_.Group | Measure-Object -Property size -Sum).Sum) / 1MB, 1)
      Say ('  ' + $_.Name + ': ' + $_.Count + ' items, ' + $mb + ' MB')
    }
  $all | Where-Object { $_.deleted } | Select-Object -First 30 | ForEach-Object { Say ('    deleted here: ' + $_.name) }
  $all | Where-Object { -not $_.deleted } | Sort-Object modified -Descending | Select-Object -First 30 |
    ForEach-Object { Say ('    ' + $_.name + '  ' + $_.modified) }
}

Show-Facts 'before'
if ($Apply) {
  $lines = @((Get-Api "/rest/db/ignores?folder=$FolderId").ignore)
  foreach ($add in @('.git', '*.tmp')) { if ($lines -notcontains $add) { $lines += $add } }
  Send-Api "/rest/db/ignores?folder=$FolderId" 'Post' @{ ignore = [object[]]$lines }
  New-Item -ItemType Directory -Force -Path $Versions | Out-Null
  Send-Api "/rest/config/folders/$FolderId" 'Patch' @{ versioning = @{ type = 'trashcan'; params = @{ cleanoutDays = '30' };
                                                                     cleanupIntervalS = 3600; fsPath = $Versions; fsType = 'basic' } }
  Send-Api "/rest/config/folders/$FolderId" 'Patch' @{ type = 'sendreceive' }
  Start-Sleep -Seconds 5
  Show-Facts 'after'
}
$file = Join-Path $env:TEMP 'syncthing_two_way.txt'
$Out | Set-Content -Encoding UTF8 $file
try {
  Invoke-WebRequest -UseBasicParsing -Method Put -InFile $file -Uri 'http://172.16.136.1:8765/upload/syncthing_two_way.txt' -TimeoutSec 60 | Out-Null
  Write-Host 'uploaded to the Mac'
} catch {
  Write-Host ('upload to the Mac failed (receiver not running?): ' + $_.Exception.Message + '  -> result is in ' + $file)
}
