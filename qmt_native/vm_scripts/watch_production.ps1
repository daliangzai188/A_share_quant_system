# Read-only: every 5 minutes upload production's broker health and the last daemon log
# lines to the Mac receiver, so production's QMT link can be watched while the Mac is locked.
# Runs in the foreground of this terminal tab; close the tab (or Ctrl+C) to stop it.
$ErrorActionPreference = 'Continue'
while ($true) {
  try {
    $lines = @((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
    $lines += Get-Content C:\A_System\logs\broker_health.json -Encoding UTF8
    $lines += '--- daemon log tail'
    $lines += Get-Content C:\A_System\logs\trading_daemon.log -Tail 15 -Encoding UTF8
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
    $req = [System.Net.HttpWebRequest]::Create('http://172.16.136.1:8765/upload/production_watch.txt')
    $req.Method = 'PUT'; $req.ContentLength = $bytes.Length
    $out = $req.GetRequestStream(); $out.Write($bytes, 0, $bytes.Length); $out.Close()
    $resp = $req.GetResponse(); $resp.Close()
    (Get-Date).ToString('HH:mm:ss') + ' uploaded'
  } catch { (Get-Date).ToString('HH:mm:ss') + ' ' + $_.Exception.Message }
  Start-Sleep -Seconds 300
}
