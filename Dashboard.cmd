@echo off
cd /d "%~dp0"
python dashboard.py %*
if errorlevel 1 pause
