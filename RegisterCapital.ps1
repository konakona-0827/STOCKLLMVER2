#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
# Register only the supplied 2.13.59 x64 DLL, using the SDK's regsvr32 approach.
$sdkRoot = Join-Path $PSScriptRoot 'CapitalAPI_2.13.59'
$dllFiles = @(Get-ChildItem -LiteralPath $sdkRoot -Filter SKCOM.dll -Recurse -File |
    Where-Object { $_.Directory.Name -eq 'x64' -and $_.FullName -notlike '*CapitalAPI_v5*' })
if ($dllFiles.Count -ne 1) { throw 'Expected exactly one CapitalAPI 2.13.59 x64 SKCOM.dll.' }
$dllPath = $dllFiles[0].FullName
$registrar = Join-Path $env:WINDIR 'System32\regsvr32.exe'
$registration = Start-Process -FilePath $registrar -ArgumentList @('/s', ('"' + $dllPath + '"')) -WindowStyle Hidden -Wait -PassThru
if ($registration.ExitCode -ne 0) { throw "SKCOM registration failed, exit code $($registration.ExitCode)." }
Write-Host 'SKCOM x64 registration completed. No login or trading was performed.'
Write-Host 'Next: python capital_check.py --connect'
