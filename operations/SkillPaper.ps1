param(
    [ValidateSet('Status','Start','Resume','Pause','Stop','Report')][string]$Action = 'Status',
    [string]$Day = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$registryFile = Join-Path $projectRoot 'research-runs/active-paper.json'
if (-not (Test-Path -LiteralPath $registryFile)) {
    $registryFile = Join-Path $projectRoot 'research-runs/llm-paper-20261125/active-paper.json'
}
if (-not (Test-Path -LiteralPath $registryFile)) {
    Write-Output 'No Paper deployment is registered on this computer. See research/llm_paper/README.md; live trading uses SkillTrading-Live.cmd.'
    if ($Action -eq 'Status') { return }
    throw 'Create an isolated local Paper experiment before controlling it; remote credentials/ledgers are not copied.'
}
$deployment = Get-Content -LiteralPath $registryFile -Raw | ConvertFrom-Json
if ($deployment.mode -ne 'PAPER_ONLY') { throw 'PAPER_ONLY deployment required' }
function Assert-ChildPath([string]$candidate,[string]$parent) {
    $absolute=[IO.Path]::GetFullPath($candidate)
    $prefix=[IO.Path]::GetFullPath($parent).TrimEnd([char[]]'\/')+[IO.Path]::DirectorySeparatorChar
    if (-not $absolute.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { throw 'Path outside isolated Paper workspace' }
    return $absolute
}
$releaseRoot=Assert-ChildPath $deployment.release_root (Join-Path $projectRoot 'research-runs')
$paperRoot=Assert-ChildPath $deployment.paper_root (Join-Path $releaseRoot 'research-runs')
$controlFile=Join-Path $paperRoot 'control.json'
$statusFile=Join-Path $paperRoot 'status.json'
function Read-PaperStatus {
    if (Test-Path -LiteralPath $statusFile) { return Get-Content -LiteralPath $statusFile -Raw | ConvertFrom-Json }
    return $null
}
function Write-Control([bool]$active,[bool]$stop) {
    $control=Get-Content -LiteralPath $controlFile -Raw | ConvertFrom-Json
    if ($control.mode -ne 'PAPER_ONLY') { throw 'Invalid control mode' }
    $control.active=$active;$control.stop=$stop
    $temp=$controlFile+'.operator.tmp'
    [IO.File]::WriteAllText($temp,($control|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temp -Destination $controlFile -Force
}
function Quote-Argument([string]$value) { return '"'+$value+'"' }
$status=Read-PaperStatus
$owned=$null
if ($status) {
    $process=Get-CimInstance Win32_Process -Filter ('ProcessId = '+[int]$status.pid) -ErrorAction SilentlyContinue
    if ($process -and $process.CommandLine -match 'research\.llm_paper' -and $process.CommandLine.Contains($paperRoot)) { $owned=$process }
}
if ($Action -in @('Start','Resume')) {
    if ($owned) {
        $control=Get-Content -LiteralPath $controlFile -Raw | ConvertFrom-Json
        if ($control.stop) { throw 'Worker is exiting; wait for its exit before Start' }
        Write-Control $true $false
        Write-Output 'Existing Paper worker resumed. No new account or second worker.'
    } else {
        if (Test-Path -LiteralPath $controlFile) { Write-Control $true $false }
        $workerArgs=@('-X','utf8','-m','research.llm_paper','forward','--output',(Quote-Argument $paperRoot),
            '--strategy',(Quote-Argument (Join-Path $releaseRoot $deployment.strategy)),
            '--transport',(Quote-Argument (Join-Path $releaseRoot 'codex_cli.py')),
            '--protocol',(Quote-Argument $deployment.protocol_file),'--sources',(Quote-Argument $deployment.source_file),
            '--credentials-source',(Quote-Argument $deployment.credentials_source))
        $worker=Start-Process -FilePath $deployment.python -ArgumentList $workerArgs -WorkingDirectory $releaseRoot -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $releaseRoot ('worker-'+(Get-Date -Format 'yyyyMMddHHmmssfff')+'.stdout.txt')) `
            -RedirectStandardError (Join-Path $releaseRoot ('worker-'+(Get-Date -Format 'yyyyMMddHHmmssfff')+'.stderr.txt')) -PassThru
        $worker.Id | Set-Content -LiteralPath (Join-Path $releaseRoot 'forward-worker.pid')
        Write-Output ('Started Paper PID '+$worker.Id+'. Existing ledger retained; live orders unavailable.')
    }
} elseif ($Action -eq 'Pause') {
    Write-Control $false $false
    Write-Output 'New Paper decisions paused. Existing Paper orders remain observed until session expiry.'
} elseif ($Action -eq 'Stop') {
    Write-Control $false $true
    Write-Output 'Requested Paper worker exit. All ledgers retained.'
} elseif ($Action -eq 'Report') {
    if (-not $Day) { $Day=[TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow,'Taipei Standard Time').ToString('yyyy-MM-dd') }
    $reportPath=Join-Path $paperRoot ('reports/'+$Day+'/manual-'+(Get-Date -Format 'yyyyMMddHHmmssfff'))
    Push-Location $releaseRoot
    try {
        & $deployment.python -X utf8 -m research.llm_paper report --paper-dir $paperRoot --day $Day --output $reportPath
        if ($LASTEXITCODE -ne 0) { throw 'Read-only Paper report failed; original evidence retained' }
    } finally { Pop-Location }
    Write-Output ('Report: '+(Join-Path $reportPath 'README.md'))
} else {
    [pscustomobject]@{ mode='PAPER_ONLY';owned_worker_running=($null-ne $owned);status=$status.status;pid=$status.pid;heartbeat=$status.at;
        next_session=$status.next_session;live_orders=$false;paper_root=$paperRoot;latest_report_error=$status.last_report_error } | Format-List
    if ($status) { $status.accounts | ConvertTo-Json -Depth 6 }
}
