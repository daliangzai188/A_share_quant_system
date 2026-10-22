# Read-only: upload the QMT shadow daemon log tail to the Mac receiver.
$ErrorActionPreference = 'Stop'
foreach ($name in @('daemon_log_tail.txt', 'paper_broker_log.txt')) {
  $f = Join-Path 'C:\qn_shadow_out' $name
  if (-not (Test-Path $f)) { "missing $f"; continue }
  $bytes = [System.IO.File]::ReadAllBytes($f)
  $req = [System.Net.HttpWebRequest]::Create('http://172.16.136.1:8765/upload/' + $name)
  $req.Method = 'PUT'; $req.ContentLength = $bytes.Length
  $out = $req.GetRequestStream(); $out.Write($bytes, 0, $bytes.Length); $out.Close()
  $resp = $req.GetResponse(); $resp.Close()
  "{0} uploaded {1:N0} bytes" -f $name, $bytes.Length
}
