@echo off
setlocal
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-dashboard.ps1"
if errorlevel 1 pause
