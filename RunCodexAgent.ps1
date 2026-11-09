param(
    [string]$StateRoot,
    [string]$PythonPath,
    [switch]$ReadOnly
)
$ErrorActionPreference = 'Stop'
if ($StateRoot) {
    $env:TRADING_STATE_ROOT = (Resolve-Path -LiteralPath $StateRoot).Path
}
if (-not $PythonPath) {
    $PythonPath = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        $PythonPath = (Get-Command python -CommandType Application).Source
    }
}
$env:TRADING_MODE = 'agent'
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($ReadOnly) {
        & $PythonPath (Join-Path $PSScriptRoot 'trading_router.py') --mode agent --read-only
    } else {
        & $PythonPath (Join-Path $PSScriptRoot 'dashboard.py')
    }
    $codexAgentExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $codexAgentExitCode
