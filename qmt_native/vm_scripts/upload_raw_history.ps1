# Read-only: upload production equity-curve-stop inputs to the Mac receiver so the
# QMT-native step 13 can be validated against the exact production state.
# Uploads (one zip per part): the step-13 research dataset (incl. the factor
# history), and data\raw daily / daily_basic / limit_list / adj_factor / top-level csv.
$ErrorActionPreference = 'Stop'
$root = 'C:\A_System'
$tmp = Join-Path $env:TEMP 'as_eq_upload'
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $tmp | Out-Null
function Send-Zip($name, $paths) {
  $zip = Join-Path $tmp $name
  Compress-Archive -Path $paths -DestinationPath $zip -CompressionLevel Optimal
  $size = (Get-Item $zip).Length
  $req = [System.Net.HttpWebRequest]::Create('http://172.16.136.1:8765/upload/' + $name)
  $req.Method = 'PUT'; $req.Timeout = 1800000; $req.ReadWriteTimeout = 1800000
  $req.AllowWriteStreamBuffering = $false; $req.ContentLength = $size
  $out = $req.GetRequestStream(); $in = [System.IO.File]::OpenRead($zip)
  $in.CopyTo($out); $in.Close(); $out.Close()
  $resp = $req.GetResponse(); $resp.Close()
  Remove-Item -Force $zip
  "{0} uploaded {1:N1} MB" -f $name, ($size / 1MB)
}
foreach ($sub in @('limit_list', 'adj_factor', 'daily_basic', 'daily')) {
  Send-Zip ('raw_' + $sub + '.zip') @((Join-Path $root ('data\raw\' + $sub + '\*.csv')))
}
Send-Zip 'raw_top.zip' @((Join-Path $root 'data\raw\*.csv'), (Join-Path $root 'data\raw\stock_basic\*'))
'RAW HISTORY UPLOADED'
