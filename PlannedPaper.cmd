@echo off
cd /d "%~dp0"
start "" pythonw.exe -m quant_research.paper_dashboard --auto
