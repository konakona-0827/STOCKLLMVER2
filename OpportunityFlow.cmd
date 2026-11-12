@echo off
title TW OPPORTUNITY FLOW - NEW SYSTEM
if "%~1"=="" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0RunOpportunityFlow.ps1" -Mode Live
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0RunOpportunityFlow.ps1" %*
)
pause
