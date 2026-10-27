@echo off
setlocal
cd /d "%~dp0"
python -X utf8 -m support_scout doctor --require-agents
set "TASK_RESULT=%ERRORLEVEL%"
if not "%SUPPORT_DESK_NO_PAUSE%"=="1" pause
exit /b %TASK_RESULT%
