@echo off
REM ============================================================
REM  Push RUNTIME DATA from C:\A_System back to Mac share (Z:)
REM  so Mac-side analysis/backtest sees fresh positions/reports.
REM  Run after market close, or register in Task Scheduler
REM  (e.g. daily 15:30). /XO = only copy newer files.
REM ============================================================
set SRC=C:\A_System
set DST=Z:

robocopy %SRC%\data    %DST%\data    /E /XO
robocopy %SRC%\reports %DST%\reports /E /XO
robocopy %SRC%\logs    %DST%\logs    /E /XO

echo.
echo Data push done.
