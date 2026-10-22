# Read-only: every 2 minutes upload the tail of today's QMT strategy output (the live
# A_System copy prints there), the push forwarder log and the external-check run log to the
# Mac receiver, so the live system can be watched without touching the QMT window.
# Runs in the foreground of this terminal tab; close the tab (or Ctrl+C) to stop it.
$ErrorActionPreference = 'Continue'
$receiver = 'http://172.16.136.1:8765/upload/'

function Tail-Bytes($path, $max) {
  # QMT keeps the file open for writing: share it and read the length from the handle
  $fs = [System.IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
  try {
    $start = [Math]::Max(0, $fs.Length - $max)
    $fs.Seek($start, 'Begin') | Out-Null
    $buf = New-Object byte[] ($fs.Length - $start)
    $n = $fs.Read($buf, 0, $buf.Length)
    if ($n -lt $buf.Length) { [Array]::Resize([ref]$buf, $n) }
    return ,$buf  # the comma keeps PowerShell from unrolling the array
  } finally { $fs.Close() }
}

function Put($name, [byte[]]$bytes) {
  $req = [System.Net.HttpWebRequest]::Create($receiver + $name)
  $req.Method = 'PUT'; $req.ContentLength = $bytes.Length
  $out = $req.GetRequestStream(); $out.Write($bytes, 0, $bytes.Length); $out.Close()
  $resp = $req.GetResponse(); $resp.Close()
}

while ($true) {
  try {
    $day = (Get-Date).ToString('yyyyMMdd')
    $qmt = "C:\QMT\GJQMT\userdata\log\XtClient_FormulaOutput_$day.log"
    if (Test-Path $qmt) { Put 'live_log_tail.txt' (Tail-Bytes $qmt 3000000) }
    $fw = 'C:\A_System\logs\qmt_notify_forwarder.log'
    if (Test-Path $fw) { Put 'forwarder_tail.txt' (Tail-Bytes $fw 60000) }
    $checks = Join-Path $env:LOCALAPPDATA 'A_System\qmt_native\external_checks'
    $run = Get-ChildItem $checks -Filter 'run_*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -Last 1
    if ($run) {
      $done = @(Get-ChildItem $checks -Filter 'done_*.json' -ErrorAction SilentlyContinue | ForEach-Object { $_.Name }) -join ' '
      $head = [System.Text.Encoding]::UTF8.GetBytes("$($run.Name) | done: $done`n")
      Put 'external_checks_tail.txt' ([byte[]]($head + (Tail-Bytes $run.FullName 60000)))
    }
    (Get-Date).ToString('HH:mm:ss') + ' uploaded'
  } catch { (Get-Date).ToString('HH:mm:ss') + ' ' + $_.Exception.Message }
  Start-Sleep -Seconds 120
}
