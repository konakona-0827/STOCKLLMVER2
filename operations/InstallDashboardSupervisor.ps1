param(
    [string]$StateRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = 'STOCKLLM Dashboard Supervisor'
)

$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$statePath = (Resolve-Path -LiteralPath $StateRoot).Path
$pythonwCommand = Get-Command pythonw.exe -ErrorAction Stop
$pythonw = $pythonwCommand.Source
$supervisor = Join-Path $codeRoot 'operations/dashboard_supervisor.py'
$strategy = Join-Path $codeRoot 'strategies/strategy_A_v2_2_calendar_aware_decisions.md'

foreach ($required in @($pythonw, $supervisor, $strategy,
        (Join-Path $statePath 'data/execution/live_execution.sqlite3'))) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required Dashboard file is missing: $required"
    }
}
if (@($pythonw, $supervisor, $strategy, $statePath) | Where-Object { $_.Contains('"') }) {
    throw 'Paths containing quotation marks are unsupported by this task action.'
}
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq 'Running') {
    throw "Scheduled task is already running: $TaskName"
}
$otherDashboard = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '^python(w)?\.exe$' -and
                   $_.CommandLine -match 'dashboard\.py' }
if ($otherDashboard) {
    throw 'A Dashboard process is already running; close it normally before starting the supervisor.'
}

$arguments = '"{0}" --state-root "{1}" --mode agent --fallback-strategy "{2}"' -f
    $supervisor, $statePath, $strategy
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $codeRoot
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
    -Settings $settings -Description 'Runs the interactive STOCKLLM Dashboard and restarts silent exits; a normal window close stops it.' `
    -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
