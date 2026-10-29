@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -Command "$a='output\discovery\latest_attempt.json'; if(Test-Path -LiteralPath $a){$last=Get-Content -Raw -LiteralPath $a | ConvertFrom-Json; Write-Host ('Last attempt: '+$last.status+' '+$last.error)}; $paths=@('output\discovery\latest.json','output\discovery\latest_discovery.json'); $items=@(foreach($p in $paths){if(Test-Path -LiteralPath $p){Get-Content -Raw -LiteralPath $p | ConvertFrom-Json}}); if(!$items.Count){Write-Host 'Run Discover.cmd first.'; exit 2}; $r=$items | Sort-Object {[DateTimeOffset]::Parse($_.as_of)} -Descending | Select-Object -First 1; Write-Host ('Saved report as-of: '+$r.as_of); Start-Process -FilePath $r.report -WindowStyle Hidden"
exit /b %ERRORLEVEL%
