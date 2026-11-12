@echo off
title TW OPPORTUNITY FLOW - NEW SYSTEM
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0RunOpportunityFlow.ps1" %*
pause
