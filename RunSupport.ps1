param([string]$StateRoot, [switch]$ReadOnly)
$ErrorActionPreference = 'Stop'
if ($StateRoot) {
    $env:TRADING_STATE_ROOT = (Resolve-Path -LiteralPath $StateRoot).Path
}
$env:TRADING_MODE = 'agent'
$env:TRADING_STRATEGY_PATH = Join-Path $PSScriptRoot 'strategies/strategy_support_scout_v1.md'
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($ReadOnly) {
        & python -m support_scout live --read-only
    } else {
        & python (Join-Path $PSScriptRoot 'dashboard.py')
    }
    $supportExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $supportExitCode
