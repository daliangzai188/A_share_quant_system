@echo off
REM ============================================================
REM  Sync CODE from Mac shared drive (Z:) to local C:\A_System
REM  Run this AFTER Mac-side code changes, BEFORE restarting daemon.
REM  Runtime dirs (data/reports/logs) are NOT touched -- after the
REM  migration, C:\A_System is the source of truth for those.
REM ============================================================
set SRC=Z:
set DST=C:\A_System

robocopy %SRC%\scripts %DST%\scripts /E /XD __pycache__
robocopy %SRC%\src     %DST%\src     /E /XD __pycache__
robocopy %SRC%\config  %DST%\config  /E
robocopy %SRC%\docs    %DST%\docs    /E
copy /Y %SRC%\start_windows.py %DST%\ >nul
copy /Y %SRC%\stop_windows.py  %DST%\ >nul
copy /Y %SRC%\setup_windows.py %DST%\ >nul
copy /Y %SRC%\requirements.txt %DST%\ >nul
copy /Y %SRC%\q.bat            %DST%\ >nul

echo.
echo Code sync done. Restart daemon from %DST%.
pause
