param(
    [ValidateSet('Demo','Collect','Cycle','Watch','Live','Check')][string]$Mode = 'Demo',
    [string]$Dataset,
    [string]$State = 'local_data/opportunity_flow_state',
    [string]$Date,
    [string]$AsOf,
    [string]$Quotes,
    [string]$Events,
    [string]$Model,
    [string]$PythonPath = 'python',
    [switch]$Review,
    [string]$StateRoot,
    [switch]$ReadOnly,
    [switch]$Headless
)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    Write-Host 'TW OPPORTUNITY FLOW — 獨立研究與正式執行器'
    $flowCommand = if ($Mode -eq 'Check') { 'doctor' } else { $Mode.ToLowerInvariant() }
    $flowArgs = @('-m','opportunity_flow',$flowCommand)
    if ($Mode -eq 'Check') {
        if ($StateRoot) { $flowArgs += @('--state-root',$StateRoot) }
    } elseif ($Mode -eq 'Live') {
        if (-not $Dataset) {
            $Dataset = if (Test-Path 'local_data/official_market_20261111/manifest.json') { 'local_data/official_market_20261111' } else { 'local_data/opportunity_flow_market' }
        }
        $liveState = if ($PSBoundParameters.ContainsKey('State')) { $State } else { 'local_data/opportunity_flow_live' }
        $flowArgs += @('--dataset',$Dataset,'--state',$liveState)
        if ($StateRoot) { $flowArgs += @('--state-root',$StateRoot) }
        if ($ReadOnly) { $flowArgs += '--read-only' }
        if ($Headless) { $flowArgs += '--headless' }
        if ($Model) { $flowArgs += @('--model',$Model) }
    } elseif ($Mode -eq 'Demo') {
        $flowArgs += @('--output',('local_data/flow_demo_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff')))
    } elseif ($Mode -eq 'Collect') {
        if (-not $Date) { throw 'Collect 需要 -Date，指定台灣今日以前的完整交易日。' }
        $flowArgs += @('--date',$Date,'--output','local_data/opportunity_flow_market')
    } else {
        if (-not $Dataset) { throw 'Cycle / Watch 需要 -Dataset。' }
        $flowArgs += @('--dataset',$Dataset,'--state',$State)
        if ($Mode -eq 'Cycle') {
            if (-not $AsOf) { throw 'Cycle 需要含時區的 -AsOf。' }
            $flowArgs += @('--as-of',$AsOf)
        }
        if ($Quotes) { $flowArgs += @('--quotes',$Quotes) }
        if ($Events) { $flowArgs += @('--events',$Events) }
        if ($Model) { $flowArgs += @('--model',$Model) }
        if ($Review) { $flowArgs += '--llm' }
        if ($Mode -eq 'Watch') {
            $flowArgs += '--refresh-daily'
            if (-not $Quotes) { $flowArgs += '--public-quotes' }
        }
    }
    & $PythonPath @flowArgs
    if ($LASTEXITCODE -ne 0) { throw "TW Opportunity Flow 結束碼：$LASTEXITCODE" }
} finally {
    Pop-Location
}
