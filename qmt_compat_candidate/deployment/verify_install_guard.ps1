# Run only after copying this script beside manifest.json and scripts\.
# This installs the existing guard; it does not start a trading daemon.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$p = $PSScriptRoot
$failureCode = 'PRECHECK_FAILED'
$phase = 'PRECHECK'
$exitCode = 1
$privateHome = $null
$installerProcess = $null
$audit = [ordered]@{
    schema_version = 1
    scope = 'EXISTING_GUARD_SYNC_RECOVERY_INSTALLATION'
    started_at_utc = [DateTime]::UtcNow.ToString('o')
    result = 'FAIL'
    failure_code = $null
    error_type = $null
    phase = 'PRECHECK'
    package_verified = $false
    dependencies_verified = $false
    installation_invoked = $false
    installation_report_verified = $false
    installed_public_files_verified = $false
    status_fresh = $false
    same_task_running = $false
    guard_process_count = $null
    guard_status_matches_process = $false
    guard_enabled_preserved = $false
    native_only_preserved = $false
    manual_stop_preserved = $false
    qmt_process_preserved = $false
    sync_recovery_enabled = $false
    sync_local_healthy = $false
    sync_state = 'UNKNOWN'
    sends_orders = $false
    starts_trading_daemon = $false
    remote_device_connection_verified = $false
    folder_sync_verified = $false
    post_oct23_automatic_trading_ready = $false
    audit_saved = $false
}

function Stop-Install([string] $Code) {
    $script:failureCode = $Code
    throw [System.InvalidOperationException]::new($Code)
}

function Read-JsonFile([string] $Path) {
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Get-CurrentFullQmt {
    return @(Get-CimInstance Win32_Process -Filter "Name='XtItClient.exe'")
}

function Get-CurrentGuard([string] $PrivateRoot) {
    $scriptPath = Join-Path $PrivateRoot 'qmt_client_guard.py'
    return @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($scriptPath) -and $_.CommandLine.Contains('--run') })
}

function Assert-PublicFile([string] $Path, [object] $Expected) {
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) { Stop-Install 'PUBLIC_FILE_MISSING' }
    $item = Get-Item -LiteralPath $Path
    if ($item.Length -ne $Expected.bytes -or
        (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ine $Expected.sha256) {
        Stop-Install 'PUBLIC_FILE_MISMATCH'
    }
}

try {
    if (!$env:LOCALAPPDATA -or !(Test-Path -LiteralPath $env:LOCALAPPDATA -PathType Container)) {
        Stop-Install 'LOCAL_PROFILE_UNAVAILABLE'
    }
    $privateHome = Join-Path $env:LOCALAPPDATA 'A_System\qmt_client_recovery'
    $python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
    $pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
    $configPath = Join-Path $privateHome 'config.json'
    $statusPath = Join-Path $privateHome 'status.json'
    $manifestPath = Join-Path $p 'manifest.json'
    $project = 'C:\A_System'
    $expectedFullExe = 'C:\QMT\GJQMT\bin.x64\XtItClient.exe'
    foreach ($path in @($python, $pythonw, $configPath, $manifestPath)) {
        if (!(Test-Path -LiteralPath $path -PathType Leaf)) { Stop-Install 'DEPENDENCY_MISSING' }
        if ((Get-Item -LiteralPath $path).Length -le 0) { Stop-Install 'DEPENDENCY_EMPTY' }
    }
    if (!(Test-Path -LiteralPath $project -PathType Container)) { Stop-Install 'PROJECT_MISSING' }
    if ((Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash -ine
        '9d654decab1bee2af99755240f238a1f9a60f2eb6e3f5ebbd4754dd5d4962433') {
        Stop-Install 'MANIFEST_MISMATCH'
    }
    $manifest = Read-JsonFile $manifestPath
    $expectedNames = @(
        'scripts/qmt_client_guard.py', 'scripts/qmt_win32_login.py',
        'scripts/qmt_login_controls.ps1', 'scripts/configure_qmt_credentials.ps1',
        'scripts/open_sync_status.py', 'install_existing_guard.cmd', 'README.txt'
    )
    $manifestNames = @($manifest.files.PSObject.Properties.Name)
    if ($manifestNames.Count -ne 7 -or @($manifestNames | Where-Object { $_ -notin $expectedNames }).Count -ne 0) {
        Stop-Install 'MANIFEST_INVENTORY_MISMATCH'
    }
    foreach ($name in $expectedNames) {
        Assert-PublicFile (Join-Path $p $name) $manifest.files.$name
    }
    $audit.package_verified = $true

    foreach ($name in @('STHOMEDIR', 'STCONFDIR', 'STDATADIR', 'STGUIADDRESS', 'STGUIAPIKEY', 'STPAUSED', 'STUNPAUSED')) {
        if ([Environment]::GetEnvironmentVariable($name, 'Process')) { Stop-Install 'SYNC_ENV_OVERRIDE_PRESENT' }
    }
    $syncIdentity = Join-Path $env:LOCALAPPDATA 'Syncthing'
    $syncExe = Join-Path $env:LOCALAPPDATA 'Programs\Syncthing\syncthing.exe'
    foreach ($path in @($syncExe, (Join-Path $syncIdentity 'config.xml'),
        (Join-Path $syncIdentity 'cert.pem'), (Join-Path $syncIdentity 'key.pem'))) {
        if (!(Test-Path -LiteralPath $path -PathType Leaf)) { Stop-Install 'SYNC_EXISTING_DEPENDENCY_MISSING' }
        if ((Get-Item -LiteralPath $path).Length -le 0) { Stop-Install 'SYNC_EXISTING_DEPENDENCY_EMPTY' }
    }

    $configBefore = Read-JsonFile $configPath
    $qmtExe = [string]$configBefore.qmt_exe
    if ($configBefore.enabled -isnot [bool] -or $configBefore.login_mode -ne 'native_only' -or
        $configBefore.project_root -ine $project -or $qmtExe -ine $expectedFullExe) {
        Stop-Install 'CURRENT_GUARD_CONFIGURATION_MISMATCH'
    }
    # A disabled guard must not be silently enabled just to satisfy this run.
    if (!$configBefore.enabled) { Stop-Install 'GUARD_DISABLED_REQUIRES_REVIEW' }
    $enabledBefore = $configBefore.enabled
    $qmtBefore = @(Get-CurrentFullQmt)
    if ($qmtBefore.Count -ne 1 -or $qmtBefore[0].ExecutablePath -ine $qmtExe) {
        Stop-Install 'CURRENT_FULL_QMT_MISMATCH'
    }
    $qmtProcessId = $qmtBefore[0].ProcessId
    $taskBefore = @(Get-ScheduledTask -TaskName 'A_System_QmtClientGuard' -ErrorAction Stop)
    if ($taskBefore.Count -ne 1 -or @($taskBefore[0].Actions).Count -ne 1 -or
        !$taskBefore[0].Actions[0].Arguments.Contains((Join-Path $privateHome 'qmt_client_guard.py')) -or
        !$taskBefore[0].Actions[0].Arguments.Contains('--run')) {
        Stop-Install 'EXISTING_GUARD_TASK_MISMATCH'
    }
    $guardBefore = @(Get-CurrentGuard $privateHome)
    if ($guardBefore.Count -gt 1 -or ($guardBefore.Count -eq 1 -and [string]$taskBefore[0].State -ne 'Running')) {
        Stop-Install 'EXISTING_GUARD_PROCESS_UNVERIFIED'
    }
    $manualPath = Join-Path $project '.manual_stop.json'
    $manualBefore = Test-Path -LiteralPath $manualPath -PathType Leaf
    $manualHash = if ($manualBefore) { (Get-FileHash -LiteralPath $manualPath -Algorithm SHA256).Hash } else { '' }
    $audit.dependencies_verified = $true

    $phase = 'INSTALL'
    $failureCode = 'INSTALL_EXECUTION_FAILED'
    $installStarted = [DateTime]::UtcNow
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $python
    $startInfo.Arguments = '"' + (Join-Path $p 'scripts\qmt_client_guard.py') + '" --install --project "' + $project + '" --qmt-exe "' + $qmtExe + '"'
    $startInfo.WorkingDirectory = $p
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $installerProcess = New-Object System.Diagnostics.Process
    $installerProcess.StartInfo = $startInfo
    $audit.installation_invoked = $true
    if (!$installerProcess.Start()) { Stop-Install 'INSTALL_PROCESS_NOT_STARTED' }
    $stdoutTask = $installerProcess.StandardOutput.ReadToEndAsync()
    $stderrTask = $installerProcess.StandardError.ReadToEndAsync()
    if (!$installerProcess.WaitForExit(90000)) {
        # Outcome is unknown. Do not kill or retry the installer, guard, or QMT.
        Stop-Install 'INSTALL_TIMEOUT_OUTCOME_UNKNOWN'
    }
    $installStdout = $stdoutTask.GetAwaiter().GetResult()
    $installStderr = $stderrTask.GetAwaiter().GetResult()
    if ($installerProcess.ExitCode -ne 0) { Stop-Install 'INSTALL_RETURNED_FAILURE' }
    $installReport = $installStdout | ConvertFrom-Json
    if ($installReport.installed -ne $true -or $installReport.task -ne 'A_System_QmtClientGuard' -or
        $installReport.sync_device_identity_changed -ne $false) {
        Stop-Install 'INSTALL_REPORT_UNVERIFIED'
    }
    $audit.installation_report_verified = $true

    $phase = 'POSTCHECK'
    $failureCode = 'POSTCHECK_FAILED'
    foreach ($name in @('qmt_client_guard.py', 'qmt_win32_login.py', 'qmt_login_controls.ps1', 'configure_qmt_credentials.ps1')) {
        Assert-PublicFile (Join-Path $privateHome $name) $manifest.files.('scripts/' + $name)
    }
    Assert-PublicFile (Join-Path $env:LOCALAPPDATA 'A_System\sync_tools\open_sync_status.py') $manifest.files.'scripts/open_sync_status.py'
    $audit.installed_public_files_verified = $true
    $configAfter = Read-JsonFile $configPath
    $audit.guard_enabled_preserved = ($configAfter.enabled -is [bool] -and $configAfter.enabled -eq $enabledBefore)
    $audit.native_only_preserved = ($configAfter.login_mode -eq 'native_only')
    if (!$audit.guard_enabled_preserved -or !$audit.native_only_preserved -or
        $configAfter.qmt_exe -ine $qmtExe -or $configAfter.project_root -ine $project) {
        Stop-Install 'INSTALLED_CONFIGURATION_CHANGED'
    }
    $audit.sync_recovery_enabled = ($configAfter.syncthing_recovery.enabled -eq $true)
    if (!$audit.sync_recovery_enabled) { Stop-Install 'SYNC_RECOVERY_DISABLED' }
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    $status = $null
    do {
        if ((Test-Path -LiteralPath $statusPath -PathType Leaf) -and
            (Get-Item -LiteralPath $statusPath).LastWriteTimeUtc -ge $installStarted) {
            try { $status = Read-JsonFile $statusPath } catch { $status = $null }
            if ($null -ne $status) {
                $observedGuards = @(Get-CurrentGuard $privateHome)
                if ($observedGuards.Count -eq 1 -and $status.pid -eq $observedGuards[0].ProcessId) { break }
                $status = $null
            }
        }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $deadline)
    $audit.status_fresh = ($null -ne $status)
    if (!$audit.status_fresh) { Stop-Install 'NEW_GUARD_STATUS_TIMEOUT' }
    $taskAfter = @(Get-ScheduledTask -TaskName 'A_System_QmtClientGuard' -ErrorAction Stop)
    $audit.same_task_running = ($taskAfter.Count -eq 1 -and [string]$taskAfter[0].State -eq 'Running' -and
        @($taskAfter[0].Actions).Count -eq 1 -and $taskAfter[0].Actions[0].Execute -ieq $pythonw -and
        $taskAfter[0].Actions[0].Arguments.Contains((Join-Path $privateHome 'qmt_client_guard.py')) -and
        $taskAfter[0].Actions[0].Arguments.Contains('--run'))
    $guardAfter = @(Get-CurrentGuard $privateHome)
    $audit.guard_process_count = $guardAfter.Count
    $audit.guard_status_matches_process = ($guardAfter.Count -eq 1 -and $status.pid -eq $guardAfter[0].ProcessId)
    if (!$audit.same_task_running -or !$audit.guard_status_matches_process) { Stop-Install 'GUARD_NOT_UNIQUELY_RUNNING' }
    $qmtAfter = @(Get-CurrentFullQmt)
    $audit.qmt_process_preserved = ($qmtAfter.Count -eq 1 -and $qmtAfter[0].ProcessId -eq $qmtProcessId -and
        $qmtAfter[0].ExecutablePath -ieq $qmtExe)
    $manualAfter = Test-Path -LiteralPath $manualPath -PathType Leaf
    $audit.manual_stop_preserved = ($manualBefore -eq $manualAfter)
    if ($manualBefore -and $manualAfter) {
        $audit.manual_stop_preserved = $audit.manual_stop_preserved -and
            ((Get-FileHash -LiteralPath $manualPath -Algorithm SHA256).Hash -eq $manualHash)
    }
    if (!$audit.qmt_process_preserved -or !$audit.manual_stop_preserved) { Stop-Install 'PRESERVATION_CHECK_FAILED' }
    if ($status.enabled -ne $enabledBefore -or $status.login_mode -ne 'native_only' -or
        $status.manual_stop -ne $manualAfter -or $status.sends_orders -isnot [bool] -or
        $status.sends_orders -ne $false -or $status.starts_trading_daemon -isnot [bool] -or
        $status.starts_trading_daemon -ne $false) { Stop-Install 'GUARD_STATUS_CONTRACT_MISMATCH' }
    $audit.sends_orders = $status.sends_orders
    $audit.starts_trading_daemon = $status.starts_trading_daemon
    $audit.sync_local_healthy = ($status.syncthing.healthy -is [bool] -and $status.syncthing.healthy)
    if ([string]$status.syncthing.state -match '^[A-Za-z0-9_]{1,64}$') { $audit.sync_state = [string]$status.syncthing.state }
    if (!$audit.sync_local_healthy) { Stop-Install 'SYNC_LOCAL_HEALTH_UNVERIFIED' }
    $audit.result = 'PASS'
    $exitCode = 0
} catch {
    $audit.failure_code = $failureCode
    $audit.error_type = $_.Exception.GetType().Name
} finally {
    if ($null -ne $installerProcess) {
        try { $installerProcess.Dispose() } catch {
            $audit.result = 'FAIL'
            $audit.failure_code = 'INSTALLER_HANDLE_CLOSE_FAILED'
            $audit.error_type = $_.Exception.GetType().Name
            $exitCode = 1
        }
    }
    $audit.phase = $phase
    $audit.finished_at_utc = [DateTime]::UtcNow.ToString('o')
    try {
        if ($privateHome -and (Test-Path -LiteralPath $privateHome -PathType Container)) {
            $audit.audit_saved = $true
            $auditPath = Join-Path $privateHome 'installation_audit.json'
            $temporaryAudit = $auditPath + '.tmp'
            $utf8 = [System.Text.UTF8Encoding]::new($false)
            [System.IO.File]::WriteAllText($temporaryAudit, ($audit | ConvertTo-Json -Depth 5), $utf8)
            Move-Item -LiteralPath $temporaryAudit -Destination $auditPath -Force
        }
    } catch {
        $audit.audit_saved = $false
        $audit.result = 'FAIL'
        $audit.failure_code = 'INSTALLER_AUDIT_SAVE_FAILED'
        $audit.error_type = $_.Exception.GetType().Name
        $exitCode = 1
    }
    # Only this allowlisted report is printed. Native output/config are not.
    $audit | ConvertTo-Json -Depth 5 -Compress | Write-Output
}
exit $exitCode
