@echo off
setlocal

set "ROOT=%~dp0"
set "ROOT_NO_SLASH=%ROOT:~0,-1%"
set "PY=%USERPROFILE%\.venv_a_system_312_x64\Scripts\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\.venv_a_system_312\Scripts\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\.venv_a_system\Scripts\python.exe"

if "%~1"=="" goto help
if /I "%~1"=="check" goto check
if /I "%~1"=="account" goto account
if /I "%~1"=="probe" goto probe
if /I "%~1"=="preview" goto preview
if /I "%~1"=="plan" goto plan
if /I "%~1"=="env" goto env
if /I "%~1"=="init-env" goto initenv
if /I "%~1"=="findpath" goto findpath
if /I "%~1"=="scan-qmt" goto scanqmt
if /I "%~1"=="install-xtquant" goto installxtquant
if /I "%~1"=="pyinfo" goto pyinfo
if /I "%~1"=="deps" goto deps
goto help

:check
cd /d "%ROOT%"
"%PY%" -B scripts\check_qmt_live_readiness.py
goto end

:account
cd /d "%ROOT%"
"%PY%" -B scripts\qmt_account_check.py
goto end

:probe
cd /d "%ROOT%"
"%PY%" -B scripts\probe_qmt_connection.py
goto end

:preview
cd /d "%ROOT%"
"%PY%" -B scripts\preview_live_orders.py --planned-orders latest
goto end

:plan
cd /d "%ROOT%"
"%PY%" -B scripts\run_paper_ab_filtered_daily_ops.py --top-n 10
goto end

:env
cd /d "%ROOT%"
notepad .env
goto end

:initenv
cd /d "%ROOT%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\windows_init_qmt_env.ps1" -ProjectRoot "%ROOT_NO_SLASH%"
goto end

:findpath
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Name -ne 'Z' } | ForEach-Object { Get-ChildItem -Path ($_.Name + ':\') -Directory -Filter 'userdata*' -Recurse -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName }"
goto end

:scanqmt
cd /d "%ROOT%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\windows_scan_qmt_paths.ps1" -ProjectRoot "%ROOT_NO_SLASH%"
goto end

:installxtquant
"%PY%" -m pip install xtquant
goto end

:pyinfo
"%PY%" -c "import platform,sys,struct; print('executable=', sys.executable); print('version=', sys.version); print('platform.machine=', platform.machine()); print('bits=', struct.calcsize('P')*8)"
goto end

:deps
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r "%ROOT%requirements.txt"
"%PY%" -m pip install tabulate
goto end

:help
echo Usage:
echo   q check     Run QMT readiness check
echo   q account   Run QMT readonly account check
echo   q probe     Probe QMT path/session readonly connection
echo   q preview   Preview latest planned orders
echo   q plan      Generate daily planned orders
echo   q env       Open .env in Notepad
echo   q init-env  Auto-fill QMT settings in .env
echo   q findpath  Find QMT userdata_mini path
echo   q scan-qmt  Scan QMT shortcuts, install dirs, userdata, xtquant
echo   q install-xtquant  Try installing xtquant from pip
echo   q pyinfo    Show active Python executable and architecture
echo   q deps      Install Python dependencies
echo.
echo In PowerShell, run with .\q.bat check

:end
endlocal
