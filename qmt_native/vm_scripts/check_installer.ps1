# Read-only: verify the QMT 2.1.33.0 installer (signature, hash) and describe the current QMT install.
# Uploads the report to the Mac receiver. Installs nothing, changes nothing.
$ErrorActionPreference = 'Continue'
$out = New-Object System.Collections.Generic.List[string]
$f = 'C:\A_System\installers\XtItClient_x64_GJQMT_2.1.33.0.exe'
if (Test-Path $f) {
  $out.Add("installer size: " + (Get-Item $f).Length)
  $out.Add("installer sha256: " + (Get-FileHash $f -Algorithm SHA256).Hash)
  $sig = Get-AuthenticodeSignature $f
  $out.Add("signature status: " + $sig.Status + " | " + $sig.StatusMessage)
  if ($sig.SignerCertificate) {
    $out.Add("signer: " + $sig.SignerCertificate.Subject)
    $out.Add("issuer: " + $sig.SignerCertificate.Issuer)
    $out.Add("valid: " + $sig.SignerCertificate.NotBefore + " .. " + $sig.SignerCertificate.NotAfter)
  }
  if ($sig.TimeStamperCertificate) { $out.Add("timestamp by: " + $sig.TimeStamperCertificate.Subject) }
} else { $out.Add("installer not synced yet") }
$q = Get-Process XtItClient -ErrorAction SilentlyContinue | Select-Object -First 1
$out.Add("running QMT: " + $q.Path + " pid " + $q.Id)
$exe = 'C:\QMT\GJQMT\bin.x64\XtItClient.exe'
if (Test-Path $exe) { $out.Add("current QMT version: " + (Get-Item $exe).VersionInfo.FileVersion + " / " + (Get-Item $exe).VersionInfo.ProductVersion) }
Get-ChildItem 'C:\QMT\GJQMT\bin.x64' -Filter 'python*' -ErrorAction SilentlyContinue | ForEach-Object { $out.Add("current python file: " + $_.Name + " " + $_.Length) }
foreach ($d in 'C:\QMT\GJQMT\bin.x64','C:\QMT\GJQMT\userdata','C:\QMT\GJQMT\python','C:\QMT\GJQMT\datadir','C:\QMT\GJQMT\userdata_mini') {
  if (Test-Path $d) { $s = (Get-ChildItem $d -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum; $out.Add(("size {0}: {1:N0} MB" -f $d, ($s/1MB))) }
}
Get-ChildItem 'C:\QMT\GJQMT' -Directory | ForEach-Object { $out.Add("top dir: " + $_.Name) }
$drive = Get-PSDrive C; $out.Add(("C: free {0:N1} GB" -f ($drive.Free/1GB)))
foreach ($z in 'C:\Program Files\7-Zip\7z.exe','C:\Program Files (x86)\7-Zip\7z.exe') { if (Test-Path $z) { $out.Add("7zip: " + $z) } }
$text = ($out -join "`n")
$text
$bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
$r = [Net.HttpWebRequest]::Create('http://172.16.136.1:8765/upload/check_installer.txt')
$r.Method = 'PUT'; $r.ContentLength = $bytes.Length
$o = $r.GetRequestStream(); $o.Write($bytes, 0, $bytes.Length); $o.Close(); $r.GetResponse().Close()
