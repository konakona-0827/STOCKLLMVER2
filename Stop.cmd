@echo off
setlocal
cd /d "%~dp0"
if not exist data\analysis mkdir data\analysis
>data\analysis\stop.signal echo STOP
echo Stop requested. The one-shot analysis exits after the current bounded call.
