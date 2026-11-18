@echo off
setlocal
title TW OPPORTUNITY FLOW - NEW SYSTEM
if "%~1"=="" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0RunOpportunityFlow.ps1" -Mode Live
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0RunOpportunityFlow.ps1" %*
)
set "flowExit=%ERRORLEVEL%"
echo Live mode verification report: %~dp0reports\opportunity_flow_runtime\latest.txt
pause
exit /b %flowExit%
