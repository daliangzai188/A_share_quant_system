# Read-only: find which files QMT writes strategy print() output to (search for a marker text); uploads the report.
param([string]$Marker = 'daily cache keeper')
$ErrorActionPreference = 'Continue'
$out = Join-Path $env:TEMP 'find_strategy_output.txt'
$r = New-Object System.Collections.Generic.List[string]
$since = (Get-Date).AddHours(-3)
foreach ($root in @('C:\QMT\GJQMT\userdata', 'C:\QMT\GJQMT\userdata_mini', 'C:\QMT\GJQMT\bin.x64', 'C:\QMT\GJQMT\python', $env:TEMP, $env:LOCALAPPDATA, $env:APPDATA)) {
  if (-not (Test-Path $root)) { continue }
  Get-ChildItem $root -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -gt $since -and $_.Length -lt 200MB -and $_.Extension -notin @('.dat', '.db', '.dll', '.exe', '.pyc') } |
    ForEach-Object {
      $hit = $false
      try {
        $fs = [IO.File]::Open($_.FullName, 'Open', 'Read', 'ReadWrite')
        $sr = New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)
        $text = $sr.ReadToEnd(); $sr.Close(); $fs.Close()
        $hit = $text.Contains($Marker)
      } catch {}
      if ($hit) { $r.Add("HIT " + $_.FullName + " size=" + $_.Length + " mtime=" + $_.LastWriteTime.ToString('HH:mm:ss')) }
    }
}
$r.Add('--- recently written files under userdata (3 h) ---')
Get-ChildItem 'C:\QMT\GJQMT\userdata' -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -gt $since } |
  Sort-Object LastWriteTime -Descending | Select-Object -First 40 |
  ForEach-Object { $r.Add("  " + $_.FullName.Substring(22) + " " + $_.Length + " " + $_.LastWriteTime.ToString('HH:mm:ss')) }
$r.Add('done')
$r | Set-Content -Path $out -Encoding UTF8
try {
  Invoke-WebRequest -UseBasicParsing -Method Put -InFile $out -Uri 'http://172.16.136.1:8765/upload/find_strategy_output.txt' -TimeoutSec 60 | Out-Null
  'uploaded find_strategy_output.txt'
} catch { 'UPLOAD FAILED: ' + $_.Exception.Message }
