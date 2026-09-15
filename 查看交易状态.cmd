@echo off
chcp 65001 >nul
cd /d C:\A_System
py -3.11 scripts\show_trading_status.py
echo.
pause
