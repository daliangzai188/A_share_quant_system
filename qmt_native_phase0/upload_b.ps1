# Upload phase-0 export results to the Mac (172.16.136.1:8765).
# Mac Syncthing is sendonly by design, so results cannot come back via sync.
# Every 2 minutes: small progress files. When export is done: one zip of everything.
$ErrorActionPreference = 'Continue'
$out = 'C:\A_System\qmt_native_phase0\out_b'
$url = 'http://172.16.136.1:8765/upload/'
function Put-File($path, $name) {
  try {
    Invoke-WebRequest -UseBasicParsing -Method Put -InFile $path -Uri ($url + $name) -TimeoutSec 900 | Out-Null
    Write-Host ((Get-Date).ToString('HH:mm:ss') + '  uploaded ' + $name)
  } catch {
    Write-Host ((Get-Date).ToString('HH:mm:ss') + '  UPLOAD FAILED ' + $name + ' : ' + $_.Exception.Message)
  }
}
Write-Host 'PHASE0B UPLOADER RUNNING. Leave this window open.'
while ($true) {
  foreach ($f in @('progress.json', 'probe.json', 'ticks_index.csv', 'errors.log', 'daily_meta.txt')) {
    $p = Join-Path $out $f
    if (Test-Path $p) { Put-File $p ('b_' + $f) }
  }
  $done = $false
  try { $done = [bool]((Get-Content (Join-Path $out 'progress.json') -Raw | ConvertFrom-Json).done) } catch {}
  if ($done) { break }
  Start-Sleep -Seconds 120
}
$zip = Join-Path $env:TEMP 'phase0_out_b.zip'
Write-Host 'Export finished. Zipping...'
Compress-Archive -Path ($out + '\*') -DestinationPath $zip -Force
Put-File $zip 'phase0_out_b.zip'
Write-Host 'ALL UPLOADED. You can close this window.'
