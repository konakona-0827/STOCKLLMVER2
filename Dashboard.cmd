@echo off
setlocal
cd /d "%~dp0"
python -X utf8 -m support_scout serve --port 8765
