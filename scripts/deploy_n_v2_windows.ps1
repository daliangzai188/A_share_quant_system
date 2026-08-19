$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DeployLog = Join-Path $ProjectRoot "logs\n_v2_deploy.log"
$ConfigPath = Join-Path $ProjectRoot "config\config.json"
$FreezePath = Join-Path $ProjectRoot "config\strategy_release_freeze.json"

function Write-DeployLog {
    param([string]$Message)
    $Line = "{0} | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    $Line | Tee-Object -FilePath $DeployLog -Append
}

Set-Content -Path $DeployLog -Value "" -Encoding UTF8
Write-DeployLog "CHECK_START"

$VersionReady = Select-String `
    -Path $ConfigPath `
    -SimpleMatch '"strategy_version": "N_two_branch_retreat_plus_mixed_amount_v2"' `
    -Quiet
$TradeCountReady = Select-String `
    -Path $ConfigPath `
    -SimpleMatch '"trade_count": 174' `
    -Quiet
$ReleaseReady = Select-String `
    -Path $FreezePath `
    -SimpleMatch '"release_id": "portfolio-20260819-n-v2-live-v7.0"' `
    -Quiet

if (-not ($VersionReady -and $TradeCountReady -and $ReleaseReady)) {
    $GateFailure = "SYNC_GATE_FAIL version={0} trade_count={1} release={2}" -f $VersionReady, $TradeCountReady, $ReleaseReady
    Write-DeployLog $GateFailure
    throw "N_V2_SYNC_NOT_READY"
}

Write-DeployLog "SYNC_GATE_PASS"
Write-DeployLog "STOP_OLD_DAEMON"
Set-Location $ProjectRoot
& py -3.11 stop_windows.py 2>&1 | Tee-Object -FilePath $DeployLog -Append
if ($LASTEXITCODE -ne 0) {
    throw "STOP_WINDOWS_FAILED exit_code=$LASTEXITCODE"
}

Start-Sleep -Seconds 3
Write-DeployLog "START_N_V2_DAEMON"
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
