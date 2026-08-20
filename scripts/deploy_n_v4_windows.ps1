param([switch]$PreflightOnly)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DeployLog = Join-Path $ProjectRoot "logs\n_v4_deploy.log"

function Write-DeployLog {
    param([string]$Message)
    $Line = "{0} | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    $Line | Tee-Object -FilePath $DeployLog -Append
}

function Invoke-LoggedPython {
    param(
        [string]$StepName,
        [string[]]$CommandArguments
    )

    # Windows PowerShell 5.1在“原生命令 | Tee-Object”管道后可能把
    # $LASTEXITCODE留成$null。必须先让py直接结束并立即锁定退出码，再写日志。
    $NativeOutput = & py -3.11 @CommandArguments 2>&1
    $NativeExitCode = $LASTEXITCODE
    $NativeOutput | Tee-Object -FilePath $DeployLog -Append | Out-Host
    if ($null -eq $NativeExitCode) {
        Write-DeployLog "PYTHON_EXIT_CODE_MISSING step=$StepName"
        throw "PYTHON_EXIT_CODE_MISSING step=$StepName"
    }
    return [int]$NativeExitCode
}

Set-Content -Path $DeployLog -Value "" -Encoding UTF8
Set-Location $ProjectRoot
Write-DeployLog "CHECK_START"

# 先执行只读的完整发布认证。失败时旧daemon继续运行，不先制造实盘空窗。
$ReleaseExitCode = Invoke-LoggedPython `
    -StepName "live_release" `
    -CommandArguments @("scripts\validate_n_v4_live_release.py")
if ($ReleaseExitCode -ne 0) {
    Write-DeployLog "LIVE_RELEASE_GATE_FAIL exit_code=$ReleaseExitCode"
    throw "N_V4_LIVE_RELEASE_NOT_READY"
}
Write-DeployLog "LIVE_RELEASE_GATE_PASS"

$AlignmentExitCode = Invoke-LoggedPython `
    -StepName "live_engine_alignment" `
    -CommandArguments @("scripts\verify_live_engine_matches_certify.py")
if ($AlignmentExitCode -ne 0) {
    Write-DeployLog "LIVE_ENGINE_ALIGNMENT_FAIL exit_code=$AlignmentExitCode"
    throw "N_V4_LIVE_ENGINE_NOT_ALIGNED"
}
Write-DeployLog "LIVE_ENGINE_ALIGNMENT_PASS"

if ($PreflightOnly) {
    Write-DeployLog "DEPLOY_PREFLIGHT_COMPLETE"
    exit 0
}

Write-DeployLog "STOP_OLD_DAEMON"
$StopExitCode = Invoke-LoggedPython `
    -StepName "stop_windows" `
    -CommandArguments @("stop_windows.py")
if ($StopExitCode -ne 0) {
    throw "STOP_WINDOWS_FAILED exit_code=$StopExitCode"
}

Start-Sleep -Seconds 3
Write-DeployLog "START_N_V4_DAEMON"
$StartExitCode = Invoke-LoggedPython `
    -StepName "start_windows" `
    -CommandArguments @("start_windows.py", "--no-tail")
if ($StartExitCode -ne 0) {
    throw "START_WINDOWS_FAILED exit_code=$StartExitCode"
}

Start-Sleep -Seconds 5
$DaemonPid = (Get-Content (Join-Path $ProjectRoot ".daemon_pid") -ErrorAction Stop).Trim()
$KeeperPid = (Get-Content (Join-Path $ProjectRoot ".keeper_pid") -ErrorAction Stop).Trim()
$DaemonProcess = Get-Process -Id ([int]$DaemonPid) -ErrorAction Stop
$KeeperProcess = Get-Process -Id ([int]$KeeperPid) -ErrorAction Stop
$CompleteMessage = "DEPLOY_COMPLETE daemon_pid={0} daemon={1} keeper_pid={2} keeper={3}" -f $DaemonPid, $DaemonProcess.ProcessName, $KeeperPid, $KeeperProcess.ProcessName
Write-DeployLog $CompleteMessage
