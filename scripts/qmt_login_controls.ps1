param(
    [ValidateSet('inspect','login')][string]$Action = 'inspect',
    [Parameter(Mandatory=$true)][string]$ExePath,
    [string]$CredentialPath
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class QmtWindowInfo {
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h,int n);
  [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h,System.Text.StringBuilder b,int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h,out Rect r);
  public struct Rect { public int left,top,right,bottom; }
}
'@
try {
    $all = @(Get-Process -Name XtItClient -ErrorAction SilentlyContinue)
    $unreadable = @($all | Where-Object { -not $_.Path })
    $matches = @($all | Where-Object { $_.Path -and [string]::Equals($_.Path,$ExePath,[StringComparison]::OrdinalIgnoreCase) })
    if($unreadable.Count -gt 0 -or $matches.Count -gt 1){
        @{kind='observation_error'} | ConvertTo-Json -Compress
        exit 0
    }
    if($matches.Count -eq 0){ @{kind='missing'} | ConvertTo-Json -Compress; exit 0 }
    $client = $matches[0]
    $hwnd = $client.MainWindowHandle
    $className = New-Object System.Text.StringBuilder 256
    [void][QmtWindowInfo]::GetClassName($hwnd,$className,256)
    $rectangle = New-Object QmtWindowInfo+Rect
    [void][QmtWindowInfo]::GetWindowRect($hwnd,[ref]$rectangle)
    $style = [QmtWindowInfo]::GetWindowLong($hwnd,-16)
    $frame = $style -band 0x00cf0000
    $kind = 'unknown'
    if($className.ToString() -eq 'Qt5QWindowIcon'){
        if($frame -eq 0 -and ($style -band 0x16000000) -eq 0x16000000){ $kind='login' }
        if($frame -eq 0x000b0000){ $kind='main' }
    }
    $payload = @{
        kind=$kind; pid=$client.Id; hwnd=$hwnd.ToInt64()
        started_at=([DateTimeOffset]$client.StartTime).ToUnixTimeSeconds()
        width=($rectangle.right-$rectangle.left); height=($rectangle.bottom-$rectangle.top)
        controls_supported=$false; captcha=$false; password_error=$false
        backend='none'; file_version=(Get-Item -LiteralPath $ExePath).VersionInfo.FileVersion
    }
    if($kind -eq 'login'){
        $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
        $nodes = @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition))
        $edits = @($nodes | Where-Object { $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Edit -and -not $_.Current.IsOffscreen })
        $passwords = @($edits | Where-Object { $_.Current.IsPassword })
        $users = @($edits | Where-Object { -not $_.Current.IsPassword })
        $buttons = @($nodes | Where-Object { $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and $_.Current.Name -eq '登录' -and -not $_.Current.IsOffscreen })
        $payload.captcha = $edits.Count -gt 2
        $payload.password_error = @($nodes | Where-Object {
            $_.Current.ControlType -ne [System.Windows.Automation.ControlType]::Edit -and
            $_.Current.Name -match '密码错误|密码不正确|密码.*锁定|账户.*锁定|账号.*锁定'
        }).Count -gt 0
        $supported = ($passwords.Count -eq 1 -and $users.Count -eq 1 -and $buttons.Count -eq 1)
        if($supported){
            $supported = $passwords[0].GetSupportedPatterns().ProgrammaticName -contains 'ValuePatternIdentifiers.Pattern'
            $supported = $supported -and ($users[0].GetSupportedPatterns().ProgrammaticName -contains 'ValuePatternIdentifiers.Pattern')
            $supported = $supported -and ($buttons[0].GetSupportedPatterns().ProgrammaticName -contains 'InvokePatternIdentifiers.Pattern')
        }
        if($supported){ $payload.controls_supported=$true; $payload.backend='uia' }
        # 国金当前版本 Qt 未暴露控件时，由 Python 原生后端进一步验证实际像素。
        $version=$payload.file_version
        if(-not $supported -and $version -match '^2[.,]1[.,]19(?:[.,]|\b)' -and $payload.width -gt 450){
            $ratio=$payload.height/[double]$payload.width
            if($ratio -ge 0.68 -and $ratio -le 0.77){
                $payload.controls_supported=$true; $payload.backend='native'
                $payload.captcha=$ratio -gt 0.73
            }
        }
        if($Action -eq 'login'){
            if($payload.captcha -or -not $supported){ throw 'REFUSE_UNVERIFIED_LOGIN' }
            Add-Type -AssemblyName System.Security
            $encrypted = Get-Content -LiteralPath $CredentialPath -Raw | ConvertFrom-Json
            $bytes = [Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String($encrypted.credentials_dpapi_b64),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
            $credentials = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
            $users[0].GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue([string]$credentials.account)
            $passwords[0].GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue([string]$credentials.password)
            # 重新检查窗口身份，避免过期控件/主界面误触。
            if([QmtWindowInfo]::GetWindowLong($hwnd,-16) -band 0x00cf0000){ throw 'WINDOW_CHANGED' }
            $buttons[0].GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            $credentials=$null
            [Array]::Clear($bytes,0,$bytes.Length)
            $payload.result='login_submitted_unverified'
        }
    }
    $payload | ConvertTo-Json -Compress
} catch {
    # 不输出异常原文；其中可能含登录控件内容。
    @{kind='observation_error'; result='controls_unsupported'} | ConvertTo-Json -Compress
    exit 1
}
