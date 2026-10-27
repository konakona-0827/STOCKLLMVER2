@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0RunSupport.ps1" %*
if errorlevel 1 pause
