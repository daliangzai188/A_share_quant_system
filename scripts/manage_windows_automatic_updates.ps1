#Requires -RunAsAdministrator
<#
.SYNOPSIS
    管理交易虚拟机的 Windows 自动更新行为。

.DESCRIPTION
    本文件必须保留 UTF-8 BOM，Windows PowerShell 5.1 才能正确解析中文注释与字符串。
    默认动作会关闭 Windows 自动下载、自动安装和自动重启，并停止/禁用
    Windows Update 服务。这样可以避免无人值守的交易虚拟机在夜间更新后
    停留在登录界面。

    使用 -Restore 可以恢复为“通知下载、服务手动启动”的可维护状态，便于在
    非交易时段人工检查并安装安全更新。脚本不会修改 QMT 或 A_System 配置。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\manage_windows_automatic_updates.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\manage_windows_automatic_updates.ps1 -Restore
#>

[CmdletBinding()]
param(
    [switch]$Restore
)

$ErrorActionPreference = "Stop"
$auPolicyPath = "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU"

function Set-DwordPolicy {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$Value
    )

    # Registry Provider 的 New-Item -Force 会重建现有键并清掉此前的值。
    # 只能在键不存在时创建，否则三次调用最终只剩最后一条禁止重启策略。
    if (-not (Test-Path -LiteralPath $auPolicyPath)) {
        New-Item -Path $auPolicyPath -Force | Out-Null
    }
    New-ItemProperty -Path $auPolicyPath -Name $Name -PropertyType DWord -Value $Value -Force | Out-Null
}

if ($Restore) {
    # 清除本脚本设置的策略，但不触碰其他 Windows Update 管理策略。
    foreach ($name in @("NoAutoUpdate", "AUOptions", "NoAutoRebootWithLoggedOnUsers")) {
        Remove-ItemProperty -Path $auPolicyPath -Name $name -ErrorAction SilentlyContinue
    }

    Set-Service -Name "wuauserv" -StartupType Manual
    Start-Service -Name "wuauserv" -ErrorAction SilentlyContinue

    Write-Host "WINDOWS_UPDATE_RESTORED"
    Get-Service -Name "wuauserv" | Select-Object Name, Status, StartType | Format-Table -AutoSize
    exit 0
}

# NoAutoUpdate=1：不自动检查、下载或安装更新。
Set-DwordPolicy -Name "NoAutoUpdate" -Value 1

# AUOptions=2：若服务被人工临时恢复，仅通知下载，不自动安装。
Set-DwordPolicy -Name "AUOptions" -Value 2

# 即使人工安装了更新，只要用户仍登录，也禁止系统擅自自动重启。
Set-DwordPolicy -Name "NoAutoRebootWithLoggedOnUsers" -Value 1

# 停止并禁用更新服务；与上面的策略构成双重保护。
Stop-Service -Name "wuauserv" -Force -ErrorAction SilentlyContinue
Set-Service -Name "wuauserv" -StartupType Disabled

$appliedPolicy = Get-ItemProperty -Path $auPolicyPath
$updateService = Get-Service -Name "wuauserv"
# 必须读回三项策略及服务状态全部通过，才输出成功；防止部分写入冒充完成。
if ($appliedPolicy.NoAutoUpdate -ne 1 -or $appliedPolicy.AUOptions -ne 2 -or
    $appliedPolicy.NoAutoRebootWithLoggedOnUsers -ne 1 -or
    $updateService.StartType -ne "Disabled" -or $updateService.Status -ne "Stopped") {
    throw "WINDOWS_UPDATE_DISABLE_VERIFICATION_FAILED"
}
Write-Host "WINDOWS_AUTOMATIC_UPDATE_DISABLED"
Write-Host (
    "POLICY_VALUES NoAutoUpdate={0} AUOptions={1} NoAutoRebootWithLoggedOnUsers={2}" -f `
        $appliedPolicy.NoAutoUpdate,
        $appliedPolicy.AUOptions,
        $appliedPolicy.NoAutoRebootWithLoggedOnUsers
)
$appliedPolicy |
    Select-Object NoAutoUpdate, AUOptions, NoAutoRebootWithLoggedOnUsers |
    Format-List
Get-Service -Name "wuauserv" |
    Select-Object Name, Status, StartType |
    Format-Table -AutoSize
