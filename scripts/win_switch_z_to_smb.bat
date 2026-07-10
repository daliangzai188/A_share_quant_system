@echo off
setlocal
REM ============================================================
REM  Switch Z: from UTM WebDAV to Mac native SMB share.
REM  SAFE: tests on Y: first; Z: is only touched after success.
REM  Prereq: Mac side File Sharing (SMB) enabled for A_System.
REM ============================================================
set MACIP=192.168.64.1
set SHARE=A_System
set MACUSER=user

echo Step 1/3: test-mapping Y: to \\%MACIP%\%SHARE% (enter Mac password when asked)
net use Y: /delete /y >nul 2>&1
net use Y: \\%MACIP%\%SHARE% /user:%MACUSER% /persistent:no
if errorlevel 1 goto :fail

if not exist Y:\config\config.json (
  echo ERROR: Y: mapped but A_System content not found on it.
  net use Y: /delete /y >nul 2>&1
  goto :fail
)
echo Test OK: A_System is reachable via SMB.
echo.

echo Step 2/3: replacing Z: ...
net use Z: /delete /y >nul 2>&1
if exist Z:\ (
  echo WARNING: Z: is still held by something else - likely UTM/SPICE WebDAV.
  echo   Shut down VM, disable UTM "Directory Sharing", boot, rerun this script.
  net use Y: /delete /y >nul 2>&1
  goto :fail
)
net use Z: \\%MACIP%\%SHARE% /user:%MACUSER% /savecred /persistent:yes
if errorlevel 1 net use Z: \\%MACIP%\%SHARE% /user:%MACUSER% /persistent:yes
net use Y: /delete /y >nul 2>&1
echo.

echo Step 3/3: verify ...
if exist Z:\config\config.json (
  echo SUCCESS: Z: now uses Mac SMB share. Restart the daemon and
  echo compare startup time with the old ~25s/204s baseline.
) else (
  echo ERROR: Z: remap failed. Check password and rerun - nothing is lost,
  echo   you can re-enable UTM directory sharing to restore the old Z:.
)
pause
exit /b 0

:fail
echo.
echo Nothing was changed on Z:. Fix the issue above and rerun.
pause
exit /b 1
