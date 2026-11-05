# Read-only: what does QMT's "启用自动初始化" do? Find the binaries and nearby strings that implement it.
$ErrorActionPreference = 'Continue'
$out = Join-Path $env:TEMP 'reinit_facts.txt'
$r = New-Object System.Collections.Generic.List[string]
$bin = 'C:\QMT\GJQMT\bin.x64'
$files = & findstr /m /s /i /c:"reinit" "$bin\*.dll" "$bin\*.exe" 2>$null
$r.Add('files containing "reinit": ' + ($files -join ', '))
foreach ($f in $files | Select-Object -First 6) {
  $bytes = [IO.File]::ReadAllBytes($f)
  foreach ($enc in @([Text.Encoding]::ASCII, [Text.Encoding]::Unicode)) {
    $txt = $enc.GetString($bytes)
    $hits = [regex]::Matches($txt, '[ -~]{0,100}(?i:re_?init|autoinit|auto_init|reinitTime|relogin|re_login|autoRestart|auto_restart)[ -~]{0,100}')
    if ($hits.Count) {
      $r.Add("=== $f ($($enc.EncodingName)) $($hits.Count) hits")
      $hits | Select-Object -First 40 | ForEach-Object { $r.Add('  ' + $_.Value.Trim()) }
    }
  }
}
$cfg = Get-ChildItem 'C:\QMT\GJQMT\userdata\users\*\Config.xml' -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
Select-String -Path $cfg, 'C:\QMT\GJQMT\userdata\users\TableConfig.xml', 'C:\QMT\GJQMT\config\xtuserconfig.xml' -Pattern 'nit|0851|08:51|2053|20:53|autoStart|AutoStart' -ErrorAction SilentlyContinue |
  Select-Object -First 30 | ForEach-Object { $r.Add('CFG ' + $_.Filename + ':' + $_.LineNumber + ' ' + $_.Line.Trim().Substring(0, [Math]::Min(200, $_.Line.Trim().Length))) }
$r.Add('done')
$r | Set-Content -Path $out -Encoding UTF8
try {
  Invoke-WebRequest -UseBasicParsing -Method Put -InFile $out -Uri 'http://172.16.136.1:8765/upload/reinit_facts.txt' -TimeoutSec 60 | Out-Null
  'uploaded reinit_facts.txt'
} catch { 'UPLOAD FAILED: ' + $_.Exception.Message }
