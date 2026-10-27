@echo off
setlocal
cd /d "%~dp0"
python -X utf8 -m support_scout observe %*
set "TASK_RESULT=%ERRORLEVEL%"
powershell -NoProfile -Command "$p=Join-Path (Get-Location) 'output\observations\latest.json'; if (Test-Path -LiteralPath $p) { $r=Get-Content -Raw -LiteralPath $p | ConvertFrom-Json; Write-Host ('Latest report: '+$r.report) }"
echo Exit code: %TASK_RESULT%
if not "%SUPPORT_DESK_NO_PAUSE%"=="1" pause
exit /b %TASK_RESULT%
