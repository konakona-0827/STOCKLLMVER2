param(
    [string]$StateRoot,
    [switch]$ReadOnly
)

$ErrorActionPreference = 'Stop'
if ($StateRoot) {
    $agentStatePath = (Resolve-Path -LiteralPath $StateRoot).Path
    foreach ($agentRequiredFile in @(
        '.env', 'config/execution_live.json',
        'data/execution/live_execution.sqlite3',
        'data/analysis/market_history.sqlite3'
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $agentStatePath $agentRequiredFile) -PathType Leaf)) {
            throw "Required account state file is missing: $agentRequiredFile"
        }
    }
    $env:TRADING_STATE_ROOT = $agentStatePath
}
$env:TRADING_MODE = 'agent'
$env:TRADING_STRATEGY_PATH = Join-Path $PSScriptRoot 'strategies/strategy_A_v2_2_calendar_aware_decisions.md'
if ($ReadOnly) {
    & python (Join-Path $PSScriptRoot 'trading_router.py') --mode agent --read-only
} else {
    & python (Join-Path $PSScriptRoot 'dashboard.py')
}
exit $LASTEXITCODE
