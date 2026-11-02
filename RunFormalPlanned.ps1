$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$entrySettings = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config\execution_live.json') -Raw | ConvertFrom-Json
if (-not $entrySettings.enabled -or -not $entrySettings.planned_limits_enabled) {
    throw '正式限價交易未授權啟用，請勿以 Paper 入口代替。'
}
$env:TRADING_MODE = 'agent'
$formalPython = (Get-Command pythonw.exe).Source
Start-Process -FilePath $formalPython -ArgumentList @('-X','faulthandler','dashboard.py','--resume-auto') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
