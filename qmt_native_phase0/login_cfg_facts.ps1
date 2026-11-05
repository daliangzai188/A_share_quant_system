# Read-only: would a QMT auto restart come back logged in? Shows login-related settings (names and
# flags only; any value that looks like a password or hash is masked). Uploads the report.
$ErrorActionPreference = 'Continue'
$out = Join-Path $env:TEMP 'login_cfg_facts.txt'
$r = New-Object System.Collections.Generic.List[string]
$files = @('C:\QMT\GJQMT\userdata\Config.xml', 'C:\QMT\GJQMT\userdata\xtuserconfig.xml', 'C:\QMT\GJQMT\config\xtuserconfig.xml') +
         @(Get-ChildItem 'C:\QMT\GJQMT\userdata\users\*\Config.xml' -ErrorAction SilentlyContinue | ForEach-Object FullName) +
         @(Get-ChildItem 'C:\QMT\GJQMT\userdata\*.xml', 'C:\QMT\GJQMT\userdata\*.ini' -ErrorAction SilentlyContinue | ForEach-Object FullName)
foreach ($f in ($files | Select-Object -Unique)) {
  if (-not (Test-Path $f)) { continue }
  Select-String -Path $f -Pattern 'auto_?login|autologin|save_?pass|savepwd|remember|restarttime|modbootstart|LoginSetting|loginInfo|is_auto' -ErrorAction SilentlyContinue |
    Select-Object -First 25 | ForEach-Object {
      $line = $_.Line.Trim()
      $line = [regex]::Replace($line, '(?i)(pass\w*|pwd\w*|token\w*|hash\w*)="[^"]*"', '$1="***"')
      $line = [regex]::Replace($line, '(?i)(m_strPassword\w*)[:=]\s*"?[0-9a-f]{16,}"?', '$1="***"')
      $r.Add($f.Substring(14) + ':' + $_.LineNumber + ' ' + $line.Substring(0, [Math]::Min(260, $line.Length)))
    }
}
$r.Add('done')
$r | Set-Content -Path $out -Encoding UTF8
try {
  Invoke-WebRequest -UseBasicParsing -Method Put -InFile $out -Uri 'http://172.16.136.1:8765/upload/login_cfg_facts.txt' -TimeoutSec 60 | Out-Null
  'uploaded login_cfg_facts.txt'
} catch { 'UPLOAD FAILED: ' + $_.Exception.Message }
