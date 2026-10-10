# A_System guest read-only diagnostic (ASCII source only, UTF-8 BOM)
# READ-ONLY: no orders, no config writes, no passwords, no account changes.
$ErrorActionPreference = 'SilentlyContinue'
$out = [ordered]@{}
$out.generated_at = (Get-Date).ToString('s')
$out.hostname     = $env:COMPUTERNAME
$out.username     = $env:USERNAME
$out.ps_version   = $PSVersionTable.PSVersion.ToString()

# --- 1. QMT / python processes ---
$out.qmt_processes = @(
  Get-Process | Where-Object { $_.ProcessName -match 'Xt|xt|qmt|QMT|Trade|thinktrader' } |
  Select-Object -First 20 | ForEach-Object {
    $st = ''
    if ($_.StartTime) { $st = $_.StartTime.ToString('s') }
    [ordered]@{ name=$_.ProcessName; pid=$_.Id; mem_mb=[math]::Round($_.WorkingSet64/1MB,1);
                start=$st; responding=$_.Responding; window_title=$_.MainWindowTitle }
  })
$out.consent_count = @(Get-Process -Name consent).Count
$out.python_count  = @(Get-Process -Name python,py,pythonw).Count

# --- 2. scheduled restart (THE key switch) ---
$roots = @()
$roots += @(Get-ChildItem -Path $env:USERPROFILE -Directory -Filter '*QMT*')
$roots += @(Get-ChildItem -Path 'C:\' -Directory -Filter '*QMT*')
$out.qmt_roots = @($roots | ForEach-Object { $_.FullName })

$cfgs = @()
foreach ($r in $roots) {
  $udir = Join-Path $r.FullName 'userdata\users'
  foreach ($f in @(Get-ChildItem -Path $udir -Recurse -Filter 'Config.xml')) {
    $txt = Get-Content -Path $f.FullName -Raw
    $ms  = [regex]::Matches($txt, '\b(modrestart|restarttimelist)="([^"]*)"')
    $acct = $f.Directory.Name
    if ($acct.Length -gt 4) { $acct = ('*' * ($acct.Length - 4)) + $acct.Substring($acct.Length - 4) }
    $cfgs += [ordered]@{
      account_masked = $acct
      trade_setting_count = ([regex]::Matches($txt,'<TradeSetting')).Count
      attrs = @($ms | ForEach-Object { $_.Groups[1].Value + '=' + $_.Groups[2].Value })
    }
  }
}
$out.scheduled_restart = $cfgs

# --- 3. VMware Tools / input devices ---
$svc = Get-Service -Name VMTools
if ($svc) { $out.vmtools_service = $svc.Status.ToString() } else { $out.vmtools_service = 'not_found' }
$out.vmtools_version = (Get-ItemProperty -Path 'HKLM:\SOFTWARE\VMware, Inc.\VMware Tools').ProductVersion
$out.pointing_devices = @(
  Get-PnpDevice -Class Mouse,HIDClass -ErrorAction SilentlyContinue | Select-Object -First 20 |
  ForEach-Object { [ordered]@{ name=$_.FriendlyName; status=$_.Status; problem=$_.ProblemCode } })
$out.bad_input_devices = @(
  Get-PnpDevice -ErrorAction SilentlyContinue |
  Where-Object { $_.Status -ne 'OK' -and $_.Class -match 'Mouse|HID|Keyboard' } |
  ForEach-Object { [ordered]@{ name=$_.FriendlyName; status=$_.Status; problem=$_.ProblemCode } })

# --- 4. backend liveness probe from guest ---
function Probe([string]$h, [int]$p, [string]$label) {
  $r = [ordered]@{ label=$label; target=("{0}:{1}" -f $h,$p) }
  try {
    $c  = New-Object System.Net.Sockets.TcpClient
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $ok = $c.ConnectAsync($h,$p).Wait(6000)
    if (-not $ok) { $r.result = 'TCP_TIMEOUT'; $c.Close(); return $r }
    $r.connect_ms = [int]$sw.ElapsedMilliseconds
    $s = $c.GetStream()
    $s.ReadTimeout = 4000
    $buf = New-Object byte[] 64
    try {
      $n = $s.Read($buf,0,64)
      if ($n -gt 0) { $r.result = ("SERVER_SENT_{0}_BYTES" -f $n) } else { $r.result = 'SERVER_CLOSED_FIN' }
    } catch { $r.result = 'SILENT_4S' }
    $c.Close()
  } catch { $r.result = 'ERR_' + $_.Exception.GetType().Name }
  return $r
}
$out.backend_probe = @(
  (Probe '114.28.170.219'  56001 'trade-unicom2'),
  (Probe 'qmt.gjzq.com.cn' 56001 'trade-domain'),
  (Probe '162.14.133.107'  56001 'trade-telecom2'),
  (Probe '139.196.27.177'  59000 'trade-center'),
  (Probe '115.231.218.79'  55310 'QUOTE-CONTROL-GROUP')
)

# --- 5. time sanity ---
$out.guest_time  = (Get-Date).ToString('s')
$out.guest_tz    = (Get-TimeZone).Id
$w32 = Get-Service -Name w32time
if ($w32) { $out.w32time = $w32.Status.ToString() } else { $out.w32time = 'not_found' }

# --- write + print ---
$dest = Join-Path $PSScriptRoot 'result.json'
$out | ConvertTo-Json -Depth 6 | Out-File -FilePath $dest -Encoding utf8
Write-Host ''
Write-Host ("DONE -> " + $dest)
Write-Host ("consent_count = " + $out.consent_count + "   VMTools = " + $out.vmtools_version + "   svc = " + $out.vmtools_service)
Write-Host 'scheduled_restart:'
if ($out.scheduled_restart.Count -eq 0) { Write-Host '  (no Config.xml found - check qmt_roots in result.json)' }
foreach ($c in $out.scheduled_restart) {
  Write-Host ("  acct=" + $c.account_masked + "  TradeSetting=" + $c.trade_setting_count + "  " + ($c.attrs -join ' | '))
}
Write-Host 'backend_probe:'
foreach ($b in $out.backend_probe) { Write-Host ("  {0,-22} {1}" -f $b.label, $b.result) }
Write-Host 'bad_input_devices:'
if ($out.bad_input_devices.Count -eq 0) { Write-Host '  (none)' }
foreach ($d in $out.bad_input_devices) { Write-Host ("  " + $d.name + " status=" + $d.status + " problem=" + $d.problem) }
