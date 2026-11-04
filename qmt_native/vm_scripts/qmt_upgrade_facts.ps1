# Read-only facts after the QMT 2.1.19 -> 2.1.33 upgrade; uploads the report to the Mac receiver.
$ErrorActionPreference = 'Continue'
$new = 'C:\QMT\GJQMT'
$old = 'C:\QMT_backup_2.1.19'
$out = Join-Path $env:TEMP 'qmt_upgrade_facts.txt'
$r = New-Object System.Collections.Generic.List[string]
function Add($s) { $r.Add([string]$s) }

foreach ($root in @($new, $old)) {
  $exe = Join-Path $root 'bin.x64\XtItClient.exe'
  if (-not (Test-Path $exe)) { Add "exe missing: $exe"; continue }
  Add ("exe " + $root + " version=" + (Get-Item $exe).VersionInfo.FileVersion + " size=" + (Get-Item $exe).Length)
  $txt = [System.Text.Encoding]::ASCII.GetString([System.IO.File]::ReadAllBytes($exe))
  $m = [regex]::Match($txt, 'requestedExecutionLevel[^>]{0,80}')
  Add ("  manifest: " + $m.Value)
}

Add '--- python runtimes (new) ---'
Get-ChildItem $new -Recurse -File -Include 'python*.dll', 'python*.exe', 'PythonFormula*.dll' -ErrorAction SilentlyContinue |
  ForEach-Object { Add ("  " + $_.FullName.Substring($new.Length) + " " + $_.Length + " " + $_.LastWriteTime.ToString('yyyy-MM-dd')) }
foreach ($p in @('bin.x64\Lib\site-packages\pandas\_version.py', 'bin.x64\Lib\site-packages\numpy\version.py')) {
  $f = Join-Path $new $p
  if (Test-Path $f) { Select-String -Path $f -Pattern 'version' | Select-Object -First 3 | ForEach-Object { Add ("  " + $p + ": " + $_.Line.Trim()) } }
}
Get-ChildItem $new -Recurse -Directory -Filter 'site-packages' -ErrorAction SilentlyContinue |
  ForEach-Object { Add ("  site-packages: " + $_.FullName.Substring($new.Length) + " pandas=" + (Test-Path (Join-Path $_.FullName 'pandas'))) }

Add '--- python settings in config files (new) ---'
Get-ChildItem $new -Recurse -File -Include *.ini, *.xml, *.json, *.cfg, *.conf -ErrorAction SilentlyContinue |
  Where-Object { $_.Length -lt 2MB -and $_.FullName -notmatch '\\datadir\\|\\userdata\\log\\' } |
  Select-String -Pattern 'python ?ver|pythonver|py_ver|python312|python36|python_version|PythonVersion|pyVersion' -ErrorAction SilentlyContinue |
  Select-Object -First 30 | ForEach-Object { Add ("  " + $_.Path.Substring($new.Length) + ":" + $_.LineNumber + " : " + $_.Line.Trim().Substring(0, [Math]::Min(160, $_.Line.Trim().Length))) }

Add '--- config files changed by the upgrade ---'
foreach ($sub in @('config', 'bin.x64\config')) {
  $a = Join-Path $old $sub; $b = Join-Path $new $sub
  if (-not (Test-Path $b)) { continue }
  Get-ChildItem $b -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.Length -lt 5MB } | ForEach-Object {
    $rel = $_.FullName.Substring($b.Length)
    $o = Join-Path $a $rel
    if (-not (Test-Path $o)) { Add ("  NEW  " + $sub + $rel) }
    elseif ((Get-FileHash $o).Hash -ne (Get-FileHash $_.FullName).Hash) { Add ("  CHG  " + $sub + $rel) }
  }
}

Add '--- userdata kept ---'
foreach ($sub in @('python', 'userdata', 'userdata_mini')) {
  foreach ($root in @($old, $new)) {
    $d = Join-Path $root $sub
    if (Test-Path $d) {
      $fs = Get-ChildItem $d -Recurse -File -ErrorAction SilentlyContinue
      Add ("  " + $d + " files=" + $fs.Count + " bytes=" + ($fs | Measure-Object Length -Sum).Sum)
    }
  }
}
Get-ChildItem (Join-Path $new 'userdata') -Recurse -File -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -notmatch '\\log\\' } |
  Select-String -Pattern 'A_SYSTEM' -List -ErrorAction SilentlyContinue | Select-Object -First 10 |
  ForEach-Object { Add ("  mentions A_SYSTEM: " + $_.Path.Substring($new.Length)) }

Add ("QMT process: " + ((Get-Process XtItClient -ErrorAction SilentlyContinue | Select-Object -First 1).Path))
Add ("switch: " + ((Get-ItemProperty 'HKCU:\Software\A_System\QmtNative' -ErrorAction SilentlyContinue).Trading))
Add 'done'
$r | Set-Content -Path $out -Encoding UTF8
try {
  Invoke-WebRequest -UseBasicParsing -Method Put -InFile $out -Uri 'http://172.16.136.1:8765/upload/qmt_upgrade_facts.txt' -TimeoutSec 60 | Out-Null
  'uploaded qmt_upgrade_facts.txt'
} catch { 'UPLOAD FAILED: ' + $_.Exception.Message }
