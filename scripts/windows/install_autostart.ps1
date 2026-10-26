# Register JiaAgent to start after the CURRENT user logs on.
#
# Default: Windows Task Scheduler, trigger "At log on", principal = current
# user, LogonType = Interactive, RunLevel = Limited. No administrator rights
# are requested. Interactive logon is deliberate: DPAPI CurrentUser credentials
# and desktop tools only work inside the user's own session.
#
# Fallback: -UseStartupFolder writes one shortcut into the current user's
# Startup folder (also no admin). Use it only if policy blocks task creation.
#
# Before creating or overwriting a same-named task/shortcut, the script reads
# the existing resource and verifies that its executable, exact arguments, and
# working directory belong to this project. A foreign or unreadable resource is
# refused, never overwritten. Nothing here changes config/tools.yaml or any
# WeChat allowlist/scope.
[CmdletBinding()]
param(
    [switch]$EnableWeChat,
    [switch]$UseStartupFolder,
    # Validate and print the plan without creating a task or shortcut.
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'autostart_common.ps1')

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Launcher = Join-Path $PSScriptRoot 'start_jia.ps1'
$TaskName = 'JiaAgent'
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path -LiteralPath $Launcher)) {
    Write-Host "ERROR: launcher not found: $Launcher"
    exit 2
}

$powershellResolution = Resolve-JiaWindowsPowerShellPath
if (-not $powershellResolution.Ok) {
    Write-Host "ERROR: Windows PowerShell 5.1 was not found at '$($powershellResolution.Path)' ($($powershellResolution.Reason))."
    Write-Host 'Refusing to create an auto-start task or shortcut with an invalid executable path.'
    exit 2
}
$powershellExe = [string]$powershellResolution.Path
$baseArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $Launcher
# Only these exact argument strings identify this project. Anything else is
# treated as a different program, even if the executable path matches.
$allowedArguments = @($baseArguments, ($baseArguments + ' -EnableWeChat'))
$arguments = $baseArguments
if ($EnableWeChat) {
    $arguments += ' -EnableWeChat'
}

if ($UseStartupFolder) {
    $startupDir = [Environment]::GetFolderPath('Startup')
    $shortcutPath = Join-Path $startupDir 'JiaAgent.lnk'
    $shortcutInfo = Read-JiaShortcutInfo -Path $shortcutPath
    $decision = Get-JiaShortcutOwnership -Info $shortcutInfo -ExpectedTarget $powershellExe -ExpectedRoot $Root -AllowedArguments $allowedArguments
    if (-not $decision.Allowed) {
        if ($decision.State -eq 'Unreadable') {
            Write-Host "ERROR: could not verify the existing startup shortcut '$shortcutPath'."
        } else {
            Write-Host "ERROR: refusing to replace startup shortcut '$shortcutPath'."
        }
        Write-Host "  $($decision.Message)"
        Write-Host '  The existing shortcut is not verifiably owned by this JiaAgent project.'
        Write-Host '  Nothing was changed. Inspect the Startup folder first.'
        exit 1
    }
    if ($WhatIf) {
        if ($decision.State -eq 'Absent') {
            Write-Host "WhatIf: would create shortcut $shortcutPath (no existing shortcut)."
        } else {
            Write-Host "WhatIf: would update shortcut $shortcutPath (ownership verified)."
        }
        Write-Host "  Target   : $powershellExe"
        Write-Host "  Arguments: $arguments"
        Write-Host "  WorkDir  : $Root"
        exit 0
    }
    $result = Invoke-JiaShortcutInstall -Info $shortcutInfo -ExpectedTarget $powershellExe -ExpectedRoot $Root -AllowedArguments $allowedArguments -CreateOrUpdate {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $powershellExe
        $shortcut.Arguments = $arguments
        $shortcut.WorkingDirectory = $Root
        $shortcut.WindowStyle = 7
        $shortcut.Description = 'Start JiaAgent at logon (current user)'
        $shortcut.Save()
    }
    if (-not $result.Allowed) {
        Write-Host "ERROR: could not install startup shortcut '$shortcutPath'."
        Write-Host "  $($result.Message)"
        exit 1
    }
    Write-Host "Installed startup shortcut: $shortcutPath"
    Write-Host "WeChat listener: $(if ($EnableWeChat) { 'enabled for this launch' } else { 'follows .env' })"
    Write-Host 'Remove it with: scripts\windows\uninstall_autostart.ps1'
    exit 0
}

$taskInfo = Read-JiaScheduledTaskInfo -TaskName $TaskName
$decision = Get-JiaTaskOwnership -Info $taskInfo -ExpectedExecute $powershellExe -ExpectedRoot $Root -AllowedArguments $allowedArguments
if (-not $decision.Allowed) {
    if ($decision.State -eq 'Unreadable') {
        Write-Host "ERROR: could not verify the existing scheduled task '$TaskName'."
    } else {
        Write-Host "ERROR: refusing to replace scheduled task '$TaskName'."
    }
    Write-Host "  $($decision.Message)"
    Write-Host '  The existing task is not verifiably owned by this JiaAgent project.'
    Write-Host '  Nothing was changed. Inspect it in Task Scheduler (root folder) first.'
    exit 1
}

if ($WhatIf) {
    if ($decision.State -eq 'Absent') {
        Write-Host "WhatIf: would register scheduled task '$TaskName' (no existing task)."
    } else {
        Write-Host "WhatIf: would update existing scheduled task '$TaskName' (ownership verified)."
    }
    Write-Host "  Execute    : $powershellExe"
    Write-Host "  Arguments  : $arguments"
    Write-Host "  WorkingDir : $Root"
    Write-Host "  Trigger    : at logon for $User"
    Write-Host '  Principal  : Interactive / Limited (no admin)'
    Write-Host '  Instances  : IgnoreNew; no restart-on-failure'
    exit 0
}

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User

$principal = New-ScheduledTaskPrincipal `
    -UserId $User `
    -LogonType Interactive `
    -RunLevel Limited

# No restart-on-failure policy on purpose: stop_jia.ps1 terminates the
# process, which would look like a task failure and could restart Jia behind
# the user's back. Failures are recorded in logs/autostart.log and reported
# by status_jia.ps1; the next logon triggers a fresh start.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$action = New-ScheduledTaskAction `
    -Execute $powershellExe `
    -Argument $arguments `
    -WorkingDirectory $Root

$result = Invoke-JiaTaskInstall -Info $taskInfo -ExpectedExecute $powershellExe -ExpectedRoot $Root -AllowedArguments $allowedArguments -CreateOrUpdate {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath '\' `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description 'Start JiaAgent after this user logs on (interactive, limited).' `
        -Force | Out-Null
}

if (-not $result.Allowed) {
    Write-Host "ERROR: could not register scheduled task '$TaskName'."
    Write-Host "  $($result.Message)"
    Write-Host ''
    Write-Host 'If Windows denied task creation, either ask an administrator to allow'
    Write-Host 'this user to create logon tasks, or use the per-user Startup folder:'
    Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -UseStartupFolder"
    if ($EnableWeChat) {
        Write-Host '  (add -EnableWeChat to that command if you want the listener)'
    }
    exit 1
}

Write-Host "Registered scheduled task '$TaskName' for $User."
Write-Host 'Trigger: at log on (interactive session, limited privileges).'
Write-Host "WeChat listener: $(if ($EnableWeChat) { 'enabled for this launch' } else { 'follows .env' })"
Write-Host "Start now with: powershell -NoProfile -ExecutionPolicy Bypass -File `"$Launcher`"$(if ($EnableWeChat) { ' -EnableWeChat' })"
Write-Host 'Check with:    scripts\windows\status_jia.ps1'
Write-Host 'Remove with:   scripts\windows\uninstall_autostart.ps1'
