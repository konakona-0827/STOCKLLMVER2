@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
python multi_scan.py %*
