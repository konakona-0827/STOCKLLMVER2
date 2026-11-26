param(
    [ValidateSet('Check','Live')][string]$Mode='Check',
    [string]$Dataset='local_data/official_market_20261111',
    [string]$StateRoot,
    [string]$PythonPath='python',
    [switch]$ReadOnly,
    [switch]$Headless
)
$ErrorActionPreference='Stop'
Push-Location $PSScriptRoot
try {
    $checkArgs=@('-X','utf8','-m','operations.skill_readiness','--dataset',$Dataset)
    if ($StateRoot) { $checkArgs+=@('--state-root',$StateRoot) }
    & $PythonPath @checkArgs
    if ($LASTEXITCODE -ne 0) { throw 'Skill launch checks failed; no live entry started' }
    if ($Mode -eq 'Check') { return }
    if ($ReadOnly) { Write-Host 'TW Trading Skill READ ONLY: no broker orders.' }
    else { Write-Host 'TW Trading Skill LIVE: existing account, risk controls, original executor and fill ledger.' }
    $parameters=@{Mode='Live';Dataset=$Dataset;State='local_data/production_skill_live';PythonPath=$PythonPath}
    if ($StateRoot) { $parameters.StateRoot=$StateRoot }
    if ($ReadOnly) { $parameters.ReadOnly=$true }
    if ($Headless) { $parameters.Headless=$true }
    # The original entry owns account locks, recovery and live authorization.
    & (Join-Path $PSScriptRoot 'RunOpportunityFlow.ps1') @parameters
} finally { Pop-Location }
