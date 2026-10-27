# Read-only: upload one QMT log file (read through a shared handle, QMT keeps it open) to the Mac receiver.
# Usage: powershell -ep bypass -f C:\A_System\qmt_native_phase0\upload_qmt_log.ps1 XtClient_20261027
param([Parameter(Mandatory=$true)][string]$Name)
$p = "C:\QMT\GJQMT\userdata\log\$Name.log"
$fs = [IO.File]::Open($p, 'Open', 'Read', 'ReadWrite')
$ms = New-Object IO.MemoryStream; $fs.CopyTo($ms); $fs.Close(); $b = $ms.ToArray()
$q = [Net.HttpWebRequest]::Create("http://172.16.136.1:8765/upload/$Name.txt")
$q.Method = 'PUT'; $q.ContentLength = $b.Length
$o = $q.GetRequestStream(); $o.Write($b, 0, $b.Length); $o.Close(); $q.GetResponse().Close()
"UP $Name $($b.Length)"
