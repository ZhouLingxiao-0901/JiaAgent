# Remove JiaAgent auto-start registration.
#
# Removes only resources that are verifiably owned by THIS project:
#   - the root-folder scheduled task named JiaAgent
#   - the shortcut <Startup>\JiaAgent.lnk (if the fallback was used)
#
# Before deleting, the script reads the executable/target, exact arguments,
# and working directory. A foreign or unreadable resource is left untouched and
# reported; it is never deleted merely because the name matches.
#
# It never deletes credentials, data, logs, or any other task/shortcut.
[CmdletBinding()]
param(
    # Also stop a currently running JiaAgent main process after unregistering.
    # The stop script performs identity checks and never kills a process tree.
    [switch]$StopRunning
)

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'autostart_common.ps1')

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Launcher = Join-Path $PSScriptRoot 'start_jia.ps1'
$TaskName = 'JiaAgent'
$powershellResolution = Resolve-JiaWindowsPowerShellPath
if (-not $powershellResolution.Ok) {
    Write-Host "ERROR: cannot verify auto-start ownership because Windows PowerShell 5.1 was not found at '$($powershellResolution.Path)' ($($powershellResolution.Reason))."
    Write-Host 'No scheduled task or startup shortcut was changed.'
    exit 2
}
$powershellExe = [string]$powershellResolution.Path
$baseArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $Launcher
$allowedArguments = @($baseArguments, ($baseArguments + ' -EnableWeChat'))

$removed = $false
$failed = $false

$taskInfo = Read-JiaScheduledTaskInfo -TaskName $TaskName
$taskResult = Invoke-JiaTaskUninstall -Info $taskInfo -ExpectedExecute $powershellExe -ExpectedRoot $Root -AllowedArguments $allowedArguments -Remove {
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath '\' -Confirm:$false
}
if (-not $taskResult.Allowed) {
    if ($taskResult.State -eq 'Unreadable') {
        Write-Host "ERROR: could not verify scheduled task '$TaskName'; it was not removed."
    } else {
        Write-Host "ERROR: refusing to remove scheduled task '$TaskName'; it does not belong to this project."
    }
    Write-Host "  $($taskResult.Message)"
    $failed = $true
} elseif ($taskResult.Changed) {
    Write-Host "Removed scheduled task: $TaskName"
    $removed = $true
}

$shortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'JiaAgent.lnk'
$shortcutInfo = Read-JiaShortcutInfo -Path $shortcutPath
$shortcutResult = Invoke-JiaShortcutUninstall -Info $shortcutInfo -ExpectedTarget $powershellExe -ExpectedRoot $Root -AllowedArguments $allowedArguments -Remove {
    Remove-Item -LiteralPath $shortcutPath -Force
}
if (-not $shortcutResult.Allowed) {
    if ($shortcutResult.State -eq 'Unreadable') {
        Write-Host "ERROR: could not verify startup shortcut '$shortcutPath'; it was not removed."
    } else {
        Write-Host "ERROR: refusing to remove startup shortcut '$shortcutPath'; it does not belong to this project."
    }
    Write-Host "  $($shortcutResult.Message)"
    $failed = $true
} elseif ($shortcutResult.Changed) {
    Write-Host "Removed startup shortcut: $shortcutPath"
    $removed = $true
}

$exitCode = 0
if ($failed) {
    $exitCode = 1
}

if ($StopRunning) {
    $stopScript = Join-Path $PSScriptRoot 'stop_jia.ps1'
    if (Test-Path -LiteralPath $stopScript) {
        & $powershellExe -NoProfile -ExecutionPolicy Bypass -File $stopScript
        if ($LASTEXITCODE -ne 0) {
            $exitCode = $LASTEXITCODE
        }
    }
}

if ($failed) {
    Write-Host 'Auto-start cleanup was incomplete; see the errors above.'
    Write-Host 'Credentials, data, and logs were not touched.'
} elseif ($removed) {
    Write-Host 'Auto-start removed. Credentials, data, and logs were not touched.'
} else {
    Write-Host 'No JiaAgent auto-start registration was found.'
}

exit $exitCode
