@echo off
chcp 65001 >nul
title WorkBuddy Login - Automatic Session Export
cd /d "%~dp0"
set "WORKBUDDY_BROWSER_CHANNEL=msedge"
py workbuddy_checkin.py --setup
echo.
echo Keep this window open to read the result above.
pause
