@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -Command "$p=Join-Path (Get-Location) 'output\observations\latest.json'; if (!(Test-Path -LiteralPath $p)) { Write-Host 'Run Observe.cmd first.'; exit 2 }; $r=Get-Content -Raw -LiteralPath $p | ConvertFrom-Json; Write-Host ('Saved report as-of: '+$r.as_of+' / '+$r.status); Start-Process -FilePath $r.report -WindowStyle Hidden"
exit /b %ERRORLEVEL%
