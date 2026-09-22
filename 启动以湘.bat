@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\qq_assistant.ps1" start
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%
