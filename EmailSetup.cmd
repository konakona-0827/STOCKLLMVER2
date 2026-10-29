@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -File "%~dp0scripts\configure-email.ps1"
