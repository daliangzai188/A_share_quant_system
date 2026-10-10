# A_System guest agent - lets the Mac side run read-only checks without you.
# Reads cmd.ps1 from this folder, runs it, writes out.txt. Ctrl+C to stop.
$ErrorActionPreference = 'Continue'
$dir  = $PSScriptRoot
$cmdF = Join-Path $dir 'cmd.ps1'
$outF = Join-Path $dir 'out.txt'
$last = ''
Write-Host 'AGENT RUNNING. Leave this window open. Ctrl+C to stop.'
Write-Host ("watching: " + $cmdF)
while ($true) {
  if (Test-Path $cmdF) {
    $c = Get-Content $cmdF -Raw -ErrorAction SilentlyContinue
    if ($c -and $c -ne $last) {
      $last = $c
      Write-Host ((Get-Date).ToString('HH:mm:ss') + '  running new command...')
      $res = ''
      try   { $res = & ([scriptblock]::Create($c)) 2>&1 | Out-String }
      catch { $res = "ERROR: " + $_.Exception.Message }
      $hdr = '=== ran at ' + (Get-Date).ToString('s') + ' ===' + [Environment]::NewLine
      ($hdr + $res) | Out-File -FilePath $outF -Encoding utf8
      Write-Host '  done -> out.txt'
    }
  }
  Start-Sleep -Seconds 3
}
