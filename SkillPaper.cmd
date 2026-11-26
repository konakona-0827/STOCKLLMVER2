@echo off
setlocal
if "%~1"=="" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0operations\SkillPaper.ps1" -Action Status
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0operations\SkillPaper.ps1" %*
)
pause
