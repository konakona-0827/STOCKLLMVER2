@echo off
setlocal
cd /d "%~dp0"
python -X utf8 -m support_scout discover %*
set "TASK_RESULT=%ERRORLEVEL%"
echo Exit code: %TASK_RESULT%
if not "%SUPPORT_DESK_NO_PAUSE%"=="1" pause
exit /b %TASK_RESULT%
