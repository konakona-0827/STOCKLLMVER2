@echo off
setlocal
title TW TRADING SKILL - LIVE EXECUTION
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0RunSkillTrading.ps1" -Mode Live %*
pause
