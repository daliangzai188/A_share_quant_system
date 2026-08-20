$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DeployLog = Join-Path $ProjectRoot "logs\n_v4_deploy.log"

function Write-DeployLog {
    param([string]$Message)
    $Line = "{0} | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    $Line | Tee-Object -FilePath $DeployLog -Append
}

Set-Content -Path $DeployLog -Value "" -Encoding UTF8
Set-Location $ProjectRoot
Write-DeployLog "CHECK_START"

# 先执行只读的完整发布认证。失败时旧daemon继续运行，不先制造实盘空窗。
& py -3.11 scripts\validate_n_v4_live_release.py 2>&1 | Tee-Object -FilePath $DeployLog -Append
if ($LASTEXITCODE -ne 0) {
    Write-DeployLog "LIVE_RELEASE_GATE_FAIL exit_code=$LASTEXITCODE"
    throw "N_V4_LIVE_RELEASE_NOT_READY"
}
Write-DeployLog "LIVE_RELEASE_GATE_PASS"

& py -3.11 scripts\verify_live_engine_matches_certify.py 2>&1 | Tee-Object -FilePath $DeployLog -Append
if ($LASTEXITCODE -ne 0) {
    Write-DeployLog "LIVE_ENGINE_ALIGNMENT_FAIL exit_code=$LASTEXITCODE"
    throw "N_V4_LIVE_ENGINE_NOT_ALIGNED"
}
Write-DeployLog "LIVE_ENGINE_ALIGNMENT_PASS"

Write-DeployLog "STOP_OLD_DAEMON"
& py -3.11 stop_windows.py 2>&1 | Tee-Object -FilePath $DeployLog -Append
if ($LASTEXITCODE -ne 0) {
    throw "STOP_WINDOWS_FAILED exit_code=$LASTEXITCODE"
}

Start-Sleep -Seconds 3
Write-DeployLog "START_N_V4_DAEMON"
& py -3.11 start_windows.py --no-tail 2>&1 | Tee-Object -FilePath $DeployLog -Append
if ($LASTEXITCODE -ne 0) {
    throw "START_WINDOWS_FAILED exit_code=$LASTEXITCODE"
}

Start-Sleep -Seconds 5
$DaemonPid = (Get-Content (Join-Path $ProjectRoot ".daemon_pid") -ErrorAction Stop).Trim()
$KeeperPid = (Get-Content (Join-Path $ProjectRoot ".keeper_pid") -ErrorAction Stop).Trim()
$DaemonProcess = Get-Process -Id ([int]$DaemonPid) -ErrorAction Stop
$KeeperProcess = Get-Process -Id ([int]$KeeperPid) -ErrorAction Stop
$CompleteMessage = "DEPLOY_COMPLETE daemon_pid={0} daemon={1} keeper_pid={2} keeper={3}" -f $DaemonPid, $DaemonProcess.ProcessName, $KeeperPid, $KeeperProcess.ProcessName
Write-DeployLog $CompleteMessage
