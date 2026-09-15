#Requires -RunAsAdministrator
<#
.SYNOPSIS
    为无人值守的交易虚拟机关闭自动休眠、闲置锁屏及更新重启，并逐项读回。
.DESCRIPTION
    保留 UTF-8 BOM，兼容 Windows PowerShell 5.1。不会启停交易程序、修改密码或配置自动登录。
    原值保存在 reports/runtime；-VerifyOnly 仅核验，不修改系统设置。
#>
[CmdletBinding()]
param([switch]$VerifyOnly)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$reportDir = Join-Path $root 'reports/runtime'
if (-not (Test-Path -LiteralPath $reportDir)) {
    New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
}

# 只写各自的目标值，绝不重建已经存在的注册表键。
$registrySettings = @(
    @{ Path='HKCU:\Control Panel\Desktop'; Name='ScreenSaveActive'; Type='String'; Value='0' },
    @{ Path='HKCU:\Control Panel\Desktop'; Name='ScreenSaverIsSecure'; Type='String'; Value='0' },
    @{ Path='HKCU:\Control Panel\Desktop'; Name='ScreenSaveTimeOut'; Type='String'; Value='0' },
    @{ Path='HKCU:\Software\Policies\Microsoft\Windows\Control Panel\Desktop'; Name='ScreenSaveActive'; Type='String'; Value='0' },
    @{ Path='HKCU:\Software\Policies\Microsoft\Windows\Control Panel\Desktop'; Name='ScreenSaverIsSecure'; Type='String'; Value='0' },
    @{ Path='HKCU:\Software\Policies\Microsoft\Windows\Control Panel\Desktop'; Name='ScreenSaveTimeOut'; Type='String'; Value='0' },
    @{ Path='HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'; Name='InactivityTimeoutSecs'; Type='DWord'; Value=0 },
    @{ Path='HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Winlogon'; Name='EnableGoodbye'; Type='DWord'; Value=0 }
)

# 采用稳定的电源设置 GUID，避免中文系统的显示名称和隐藏设置别名差异。
$powerSettings = @(
    @{ Name='display_timeout'; Sub='7516b95f-f776-4464-8c53-06167f40cc99'; Setting='3c0bc021-c8a8-4e07-a973-6b14cbcb2b7e' },
    @{ Name='sleep_timeout'; Sub='238c9fa8-0aad-41ed-83f4-97be242c8f20'; Setting='29f6c1db-86da-48c5-9fdb-f2b67b1f44da' },
    @{ Name='hibernate_timeout'; Sub='238c9fa8-0aad-41ed-83f4-97be242c8f20'; Setting='9d7815a6-7ee4-497e-8888-515a05f02364' },
    @{ Name='unattended_sleep_timeout'; Sub='238c9fa8-0aad-41ed-83f4-97be242c8f20'; Setting='7bc4a2f9-d8fc-4469-b07b-33eb785aaca0' },
    @{ Name='wake_password'; Sub='fea3413e-7e05-4911-9a71-700331f1c294'; Setting='0e796bdb-100d-47d6-a2d5-f7d2daa51f51' }
)

function Get-RegistryState {
    @($registrySettings | ForEach-Object {
        $setting = $_
        $value = $null
        if (Test-Path -LiteralPath $setting.Path) {
            $properties = Get-ItemProperty -LiteralPath $setting.Path
            $entry = $properties.PSObject.Properties[$setting.Name]
            if ($null -ne $entry) { $value = $entry.Value }
        }
        [ordered]@{ path=$setting.Path; name=$setting.Name; value=$value; expected=$setting.Value }
    })
}

function Invoke-PowerConfig {
    param([string[]]$Arguments)
    $text = & powercfg.exe @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw ('POWERCFG_FAILED: ' + ($Arguments -join ' ') + ': ' + ($text -join ' ')) }
    return ($text -join "`n")
}

if (-not ('TradingSessionScreenSaver' -as [type])) {
    Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
public static class TradingSessionScreenSaver {
    [DllImport("user32.dll", EntryPoint="SystemParametersInfoW", SetLastError=true)]
    public static extern bool Set(uint action, uint value, System.IntPtr data, uint flags);
    [DllImport("user32.dll", EntryPoint="SystemParametersInfoW", SetLastError=true)]
    public static extern bool Get(uint action, uint value, out int data, uint flags);
}
'@
}

if (-not $VerifyOnly) {
    # 修改前落盘便于审计/还原；不包含密码或其他账户资料。
    $before = [ordered]@{
        captured_at=(Get-Date).ToString('o')
        registry=(Get-RegistryState)
        active_power_scheme=(Invoke-PowerConfig -Arguments @('/getactivescheme'))
        power_details=(Invoke-PowerConfig -Arguments @('/qh', 'SCHEME_CURRENT'))
    }
    $backup = Join-Path $reportDir ('session_policy_before_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.json')
    $before | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $backup -Encoding UTF8

    foreach ($setting in $registrySettings) {
        if (-not (Test-Path -LiteralPath $setting.Path)) {
            New-Item -Path $setting.Path -Force | Out-Null
        }
        New-ItemProperty -LiteralPath $setting.Path -Name $setting.Name -PropertyType $setting.Type -Value $setting.Value -Force | Out-Null
    }
    foreach ($setting in $powerSettings) {
        Invoke-PowerConfig -Arguments @('/setacvalueindex', 'SCHEME_CURRENT', $setting.Sub, $setting.Setting, '0') | Out-Null
        Invoke-PowerConfig -Arguments @('/setdcvalueindex', 'SCHEME_CURRENT', $setting.Sub, $setting.Setting, '0') | Out-Null
    }
    Invoke-PowerConfig -Arguments @('/setactive', 'SCHEME_CURRENT') | Out-Null

    # 立即关闭当前会话的屏幕保护程序，不依赖注销或重启才能生效。
    # 策略已禁用时系统可能拒绝再次设置；以随后的系统API读回为准。
    [TradingSessionScreenSaver]::Set(17, 0, [IntPtr]::Zero, 3) | Out-Null
    & (Join-Path $PSScriptRoot 'manage_windows_automatic_updates.ps1')
}

$registry = Get-RegistryState
$failures = @()
$screenSaverActive = -1
$screenSaverRead = [TradingSessionScreenSaver]::Get(16, 0, [ref]$screenSaverActive, 0)
if (-not $screenSaverRead -or $screenSaverActive -ne 0) { $failures += 'SCREENSAVER_CURRENT_SESSION' }
foreach ($setting in $registry) {
    if ($null -eq $setting.value -or [string]$setting.value -ne [string]$setting.expected) {
        $failures += ('REGISTRY: ' + $setting.path + '/' + $setting.name)
    }
}
$power = @($powerSettings | ForEach-Object {
    $setting = $_
    $detail = Invoke-PowerConfig -Arguments @('/qh', 'SCHEME_CURRENT', $setting.Sub, $setting.Setting)
    # 每项 /qh 输出最后两个0x值分别是当前交流/直流设置，前面的可选值不是验收值。
    $values = @([regex]::Matches($detail, '0x[0-9a-fA-F]{8}') | ForEach-Object { [Convert]::ToUInt32($_.Value.Substring(2), 16) })
    $valid = $values.Count -ge 2 -and $values[-2] -eq 0 -and $values[-1] -eq 0
    if (-not $valid) { $failures += ('POWER: ' + $setting.Name) }
    [ordered]@{ name=$setting.Name; passed=$valid; detail=$detail }
})
$policy = Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
$service = Get-Service wuauserv
$updatePassed = $policy.NoAutoUpdate -eq 1 -and $policy.AUOptions -eq 2 -and $policy.NoAutoRebootWithLoggedOnUsers -eq 1 -and $service.StartType -eq 'Disabled' -and $service.Status -eq 'Stopped'
if (-not $updatePassed) { $failures += 'WINDOWS_UPDATE' }
$report = [ordered]@{
    checked_at=(Get-Date).ToString('o')
    status=$(if ($failures.Count -eq 0) { 'PASS' } else { 'FAIL' })
    registry=$registry
    screen_saver_active=$screenSaverActive
    screen_saver_read_succeeded=$screenSaverRead
    power=$power
    update=[ordered]@{ passed=$updatePassed; NoAutoUpdate=$policy.NoAutoUpdate; AUOptions=$policy.AUOptions; NoAutoRebootWithLoggedOnUsers=$policy.NoAutoRebootWithLoggedOnUsers; service_status=[string]$service.Status; service_start_type=[string]$service.StartType }
    failures=$failures
    trading_processes_restarted=$false
}
$report | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $reportDir 'windows_session_stability_latest.json') -Encoding UTF8
Write-Host ('SESSION_STABILITY_' + $report.status)
Write-Host ('REGISTRY_CHECKS=' + $registry.Count + ' POWER_CHECKS=' + $power.Count + ' UPDATE_CHECK=' + $updatePassed)
if ($failures.Count -gt 0) { throw ($failures -join '; ') }
