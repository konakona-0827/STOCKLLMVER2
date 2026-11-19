param(
    [Parameter(Mandatory=$true)][string]$StateRoot,
    [Parameter(Mandatory=$true)][string]$Dataset,
    [Parameter(Mandatory=$true)][string]$State,
    [string]$PythonPath = 'python',
    [string]$TaskName = 'STOCKLLM Opportunity Flow',
    [ValidatePattern('^[A-Za-z0-9_.:/-]+$')][string]$Model,
    [switch]$Headless,
    [switch]$RegisterOnly,
    [switch]$ValidateOnly,
    [switch]$InspectOnly
)
$ErrorActionPreference = 'Stop'
$codeRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$accountRoot = (Resolve-Path -LiteralPath $StateRoot).Path
$datasetPath = [IO.Path]::GetFullPath($Dataset)
$flowState = [IO.Path]::GetFullPath($State)
$python = (Get-Command $PythonPath -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (Test-Path -LiteralPath $pythonw -PathType Leaf) { $python = $pythonw }
foreach ($required in @('.env','config/execution_live.json','data/execution/live_execution.sqlite3','data/analysis/market_history.sqlite3')) {
    if (-not (Test-Path -LiteralPath (Join-Path $accountRoot $required) -PathType Leaf)) {
        throw "Incomplete account state: $required"
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $codeRoot 'operations/flow_supervisor.py') -PathType Leaf)) {
    throw 'FLOW supervisor is missing.'
}
if (@($codeRoot,$accountRoot,$datasetPath,$flowState,$python) | Where-Object { $_.Contains('"') }) {
    throw 'Paths containing quotation marks are unsupported.'
}
if ($TaskName.Contains('\') -or [string]::IsNullOrWhiteSpace($TaskName)) { throw 'Invalid task name.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User.Value
$arguments = '-m operations.flow_supervisor --state-root "{0}" --dataset "{1}" --state "{2}"' -f $accountRoot,$datasetPath,$flowState
if ($Headless) { $arguments += ' --headless' }
if ($Model) { $arguments += ' --model "' + $Model + '"' }
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$definition = $scheduler.NewTask(0)
$definition.RegistrationInfo.Description = 'FLOW recovery: boot/logon, minute liveness trigger, account-scoped supervisor, fresh broker reconciliation before trading.'
$definition.Principal.UserId = $sid
$definition.Principal.LogonType = 3 # InteractiveToken; no stored Windows/broker password.
$definition.Principal.RunLevel = 0
$definition.Settings.Enabled = $true
$definition.Settings.AllowDemandStart = $true
$definition.Settings.StartWhenAvailable = $true
$definition.Settings.MultipleInstances = 2 # IgnoreNew
$definition.Settings.ExecutionTimeLimit = 'PT0S'
$definition.Settings.DisallowStartIfOnBatteries = $false
$definition.Settings.StopIfGoingOnBatteries = $false
$definition.Settings.AllowHardTerminate = $false
$definition.Settings.RestartCount = 3
$definition.Settings.RestartInterval = 'PT1M'
$boot = $definition.Triggers.Create(8)
$boot.Enabled = $true
$boot.Delay = 'PT30S'
$logon = $definition.Triggers.Create(9)
$logon.Enabled = $true
$logon.UserId = $sid
$logon.Delay = 'PT15S'
$periodic = $definition.Triggers.Create(1)
$periodic.Enabled = $true
$periodic.StartBoundary = (Get-Date).AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss')
$periodic.Repetition.Interval = 'PT1M'
$periodic.Repetition.StopAtDurationEnd = $false
$action = $definition.Actions.Create(0)
$action.Path = $python
$action.Arguments = $arguments
$action.WorkingDirectory = $codeRoot
$plan = [ordered]@{
    task_name=$TaskName; trigger_count=$definition.Triggers.Count; trigger_types=@('BOOT','OWNER_LOGON','EVERY_MINUTE')
    start_when_available=$true; multiple_instances='IgnoreNew'; logon_type='InteractiveToken'
    credentials_saved=$false; automatic_force_kill=$false; executable=$python; arguments=$arguments
    working_directory=$codeRoot; requires_logged_on_owner=$true
}
if ($ValidateOnly) {
    $plan['xml'] = $definition.XmlText
    $plan | ConvertTo-Json -Depth 5
    exit 0
}
$taskFolder = $scheduler.GetFolder('\')
$existing = $null
try { $existing = $taskFolder.GetTask($TaskName) }
catch {
    $failure = $_.Exception
    while ($failure.InnerException) { $failure = $failure.InnerException }
    if ($failure.HResult -ne -2147024894) { throw }
}
function Test-CurrentOwner($userId) {
    try {
        if ($userId -like 'S-1-*') { return $userId -eq $sid }
        $account = New-Object Security.Principal.NTAccount($userId)
        return $account.Translate([Security.Principal.SecurityIdentifier]).Value -eq $sid
    } catch { return $false }
}
function Test-FlowRecoveryDefinition($old,[switch]$OwnerOnly) {
    $types = @($old.Triggers | ForEach-Object { [int]$_.Type })
    $period = @($old.Triggers | Where-Object { $_.Type -eq 1 })
    $logons = @($old.Triggers | Where-Object { $_.Type -eq 9 })
    $disabled = @($old.Triggers | Where-Object { -not $_.Enabled })
    return ($old.Actions.Count -eq 1 -and $old.Actions.Item(1).Path -eq $python -and
        $old.Actions.Item(1).Arguments -eq $arguments -and $old.Actions.Item(1).WorkingDirectory -eq $codeRoot -and
        (Test-CurrentOwner $old.Principal.UserId) -and $old.Principal.LogonType -eq 3 -and $old.Principal.RunLevel -eq 0 -and
        $old.Triggers.Count -eq $(if ($OwnerOnly) { 2 } else { 3 }) -and
        $(if ($OwnerOnly) { $types -notcontains 8 } else { $types -contains 8 }) -and $types -contains 9 -and
        $disabled.Count -eq 0 -and $logons.Count -eq 1 -and (Test-CurrentOwner $logons[0].UserId) -and
        $period.Count -eq 1 -and $period[0].Repetition.Interval -eq 'PT1M' -and
        -not $period[0].Repetition.StopAtDurationEnd -and
        $old.Settings.Enabled -and $old.Settings.AllowDemandStart -and
        $old.Settings.StartWhenAvailable -and $old.Settings.MultipleInstances -eq 2 -and
        $old.Settings.RestartCount -eq 3 -and $old.Settings.RestartInterval -eq 'PT1M' -and
        -not $old.Settings.DisallowStartIfOnBatteries -and -not $old.Settings.StopIfGoingOnBatteries -and
        -not $old.Settings.AllowHardTerminate -and $old.Settings.ExecutionTimeLimit -eq 'PT0S')
}
$fullUnchanged = $existing -and (Test-FlowRecoveryDefinition $existing.Definition)
$ownerUnchanged = $existing -and (Test-FlowRecoveryDefinition $existing.Definition -OwnerOnly)
$unchanged = $fullUnchanged -or $ownerUnchanged
$audit = Join-Path $accountRoot 'data/health/flow_recovery'
$sha = [Security.Cryptography.SHA256]::Create()
try { $key = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($accountRoot.ToLowerInvariant())))).Replace('-','').Substring(0,12) }
finally { $sha.Dispose() }
$startupLink = Join-Path ([Environment]::GetFolderPath('Startup')) ('STOCKLLM FLOW Recovery-' + $key + '.lnk')
if ($InspectOnly) {
    # Inspect the current OS objects, not the previous installation receipt.
    $shortcutValid = $false
    if (Test-Path -LiteralPath $startupLink -PathType Leaf) {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($startupLink)
        $shortcutValid = ($shortcut.TargetPath -eq $python -and $shortcut.Arguments -eq $arguments -and
            $shortcut.WorkingDirectory -eq $codeRoot)
    }
    $conflict = $existing -and -not $unchanged -and $existing.Enabled -and $existing.Definition.Triggers.Count -gt 0
    $bootAt = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
    $effective = @()
    if ($fullUnchanged) { $effective = @('BOOT','OWNER_LOGON','EVERY_MINUTE') }
    elseif ($ownerUnchanged) { $effective = @('OWNER_LOGON','EVERY_MINUTE') }
    elseif ($shortcutValid -and -not $conflict) { $effective = @('OWNER_LOGON') }
    [ordered]@{
        inspected_at = (Get-Date).ToUniversalTime().ToString('o')
        boot_at = $bootAt
        startup_verified = [bool](($unchanged -or $shortcutValid) -and -not $conflict)
        task_present = [bool]$existing
        task_definition_verified = [bool]$unchanged
        task_enabled = [bool]($existing -and $existing.Enabled)
        task_trigger_count = $(if ($existing) { $existing.Definition.Triggers.Count } else { 0 })
        task_last_result = $(if ($existing) { $existing.LastTaskResult } else { $null })
        task_last_run_at = $(if ($existing -and $existing.LastRunTime.Year -gt 2000) { $existing.LastRunTime.ToUniversalTime().ToString('o') } else { $null })
        shortcut_verified = $shortcutValid
        conflicting_task = [bool]$conflict
        effective_triggers = $effective
        requires_logged_on_owner = $true
        current_session_is_interactive = ((Get-Process -Id $PID).SessionId -gt 0)
        credentials_saved = $false
        automatic_force_kill = $false
    } | ConvertTo-Json -Depth 4
    exit 0
}
[IO.Directory]::CreateDirectory($audit) | Out-Null
$registeredTask = $false
try {
    if (-not $unchanged) {
        if ($existing) {
            [IO.File]::WriteAllText((Join-Path $audit ('task-before-' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff') + '.xml')),
                $existing.Xml, [Text.Encoding]::Unicode)
        }
        # Updating a definition does not forcibly stop an existing broker owner.
        try { $existing = $taskFolder.RegisterTaskDefinition($TaskName,$definition,6,$sid,$null,3,$null) }
        catch {
            $failure = $_.Exception
            while ($failure.InnerException) { $failure = $failure.InnerException }
            if ($failure.HResult -ne -2147024891) { throw }
            # Boot triggers can require administrator rights. Try the owner's
            # logon + minute task before falling back to a Startup shortcut.
            $definition.Triggers.Remove(1)
            $existing = $taskFolder.RegisterTaskDefinition($TaskName,$definition,6,$sid,$null,3,$null)
        }
    }
    $installed = $taskFolder.GetTask($TaskName).Definition
    if (-not ((Test-FlowRecoveryDefinition $installed) -or (Test-FlowRecoveryDefinition $installed -OwnerOnly))) {
        throw 'Scheduled task verification failed.'
    }
    $registeredTask = $true
    $plan['registration_method'] = $(if ($installed.Triggers.Count -eq 3) { 'WINDOWS_TASK_SCHEDULER' } else { 'WINDOWS_OWNER_TASK_SCHEDULER' })
    $plan['effective_triggers'] = $(if ($installed.Triggers.Count -eq 3) { @('BOOT','OWNER_LOGON','EVERY_MINUTE') } else { @('OWNER_LOGON','EVERY_MINUTE') })
    $plan['trigger_count'] = $installed.Triggers.Count
    $plan['trigger_types'] = $plan['effective_triggers']
    if (Test-Path -LiteralPath $startupLink -PathType Leaf) { Remove-Item -LiteralPath $startupLink }
} catch {
    $failure = $_.Exception
    while ($failure.InnerException) { $failure = $failure.InnerException }
    if ($failure.HResult -ne -2147024891) { throw }
    # Restricted Windows accounts can still resume in their own interactive
    # desktop after reboot/logon. Do not request or store an admin password.
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($startupLink)
    $shortcut.TargetPath = $python
    $shortcut.Arguments = $arguments
    $shortcut.WorkingDirectory = $codeRoot
    $shortcut.WindowStyle = 7
    $shortcut.Description = 'Resume FLOW supervisor after user logon; respect manual pause and original risk controls.'
    $shortcut.Save()
    if (-not (Test-Path -LiteralPath $startupLink -PathType Leaf)) { throw 'User startup fallback verification failed.' }
    $plan['registration_method'] = 'CURRENT_USER_STARTUP_FALLBACK'
    $plan['effective_triggers'] = @('OWNER_LOGON')
    $plan['startup_shortcut'] = $startupLink
    $plan['needs_admin_for_boot_task'] = $true
    $plan['task_registration_error'] = 'ACCESS_DENIED'
}
$plan['registration_changed'] = -not $unchanged
$plan['task_registered'] = $registeredTask
$plan['registered_at'] = (Get-Date).ToString('o')
$json = $plan | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText((Join-Path $audit 'task-registration.json'),$json,[Text.UTF8Encoding]::new($false))
if (-not $RegisterOnly) {
    $intent = @{desired_running=$true;reason='EXPLICIT_RECOVERY_INSTALL';changed_at=(Get-Date).ToUniversalTime().ToString('o')} | ConvertTo-Json
    $intentTemporary = Join-Path $audit ('intent-' + [guid]::NewGuid().ToString('N') + '.tmp')
    [IO.File]::WriteAllText($intentTemporary,$intent,[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $intentTemporary -Destination (Join-Path $audit 'intent.json') -Force
    if ($registeredTask) { $taskFolder.GetTask($TaskName).Run($null) | Out-Null }
    else {
        Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $codeRoot -WindowStyle Hidden | Out-Null
    }
}
$json
