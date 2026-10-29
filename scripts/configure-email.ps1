$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskSettings = Join-Path $taskRoot 'local_data/email_settings.json'
if (!(Test-Path -LiteralPath $taskSettings)) { throw 'Run python -m support_scout notify-configure YOUR_EMAIL first.' }
$taskConfig = Get-Content -Raw -LiteralPath $taskSettings | ConvertFrom-Json
$taskSecure = Read-Host 'Gmail app password (hidden; not your normal Google password)' -AsSecureString
$taskCredential = [System.Net.NetworkCredential]::new('', $taskSecure)
$taskSecret = $taskCredential.Password.Replace(' ', '').Trim()
if ($taskSecret.Length -ne 16) { throw 'Expected a 16-character Gmail app password.' }
[System.IO.File]::WriteAllText($taskConfig.password_file, $taskSecret, [System.Text.UTF8Encoding]::new($false))
$taskSecret = $null
$taskCredential = $null
Write-Host 'Saved to ignored local_data. No email was sent. Use notify-flush to deliver queued decisions.'
