# Ownership checks shared by install_autostart.ps1 and uninstall_autostart.ps1.
#
# The scripts must never overwrite or delete a same-named task/shortcut that
# belongs to another program. This file reads the existing resource, decides
# whether it is verifiably ours, and only then runs a caller-supplied
# scriptblock. It never registers or deletes anything on its own.
#
# States:
#   Absent      no resource exists; creating one is safe
#   Owned       executable, exact arguments, and working directory all match
#   Foreign     same name, but at least one identifying value differs
#   Unreadable  the resource or its properties could not be read; fail closed
#   Invalid     expected ownership data was incomplete (caller bug)
#
# Only the root task folder (\) is queried. Tasks in subfolders are never
# enumerated, modified, or deleted.

$script:JiaTaskMissingHResult = -2147024894  # 0x80070002

# The task/shortcut must run Windows PowerShell 5.1, not whichever host
# happens to run the installer. $PSHOME under PowerShell 7 points at pwsh's
# directory, where powershell.exe does not exist. Use the fixed system path and
# verify it before building any auto-start resource.
function Resolve-JiaWindowsPowerShellPath {
    param([string]$WindowsDirectory = '')
    if ([string]::IsNullOrWhiteSpace($WindowsDirectory)) {
        $WindowsDirectory = [Environment]::GetFolderPath([System.Environment+SpecialFolder]::Windows)
        if ([string]::IsNullOrWhiteSpace($WindowsDirectory)) {
            $WindowsDirectory = [Environment]::GetEnvironmentVariable('SystemRoot', 'Machine')
        }
    }
    if ([string]::IsNullOrWhiteSpace($WindowsDirectory)) {
        return [pscustomobject]@{
            Ok = $false
            Path = ''
            Reason = 'windows-directory-unavailable'
        }
    }

    $candidate = Join-Path $WindowsDirectory 'System32\WindowsPowerShell\v1.0\powershell.exe'
    try {
        $candidate = [IO.Path]::GetFullPath($candidate)
    } catch {
        return [pscustomobject]@{
            Ok = $false
            Path = [string]$candidate
            Reason = 'path-normalization-failed'
        }
    }

    try {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf -ErrorAction Stop)) {
            return [pscustomobject]@{
                Ok = $false
                Path = $candidate
                Reason = 'powershell-not-found'
            }
        }
    } catch {
        return [pscustomobject]@{
            Ok = $false
            Path = $candidate
            Reason = 'powershell-query-failed'
        }
    }

    return [pscustomobject]@{
        Ok = $true
        Path = $candidate
        Reason = ''
    }
}

function New-JiaOwnershipDecision {
    param(
        [bool]$Allowed,
        [string]$State,
        [string]$Message
    )
    return [pscustomobject]@{
        Allowed = $Allowed
        State = $State
        Message = $Message
    }
}

function New-JiaOwnershipActionResult {
    param(
        [bool]$Allowed,
        [bool]$Changed,
        [string]$State,
        [string]$Message
    )
    return [pscustomobject]@{
        Allowed = $Allowed
        Changed = $Changed
        State = $State
        Message = $Message
    }
}

function Get-JiaNormalizedPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return ''
    }
    $value = $Path.Trim()
    if ($value.Length -ge 2 -and $value.StartsWith('"') -and $value.EndsWith('"')) {
        $value = $value.Substring(1, $value.Length - 2).Trim()
    }
    # A relative target is ambiguous. Keep it relative so it will not compare
    # equal to this project's absolute paths.
    if (-not [System.IO.Path]::IsPathRooted($value)) {
        return $value.TrimEnd('\', '/')
    }
    try {
        $full = [System.IO.Path]::GetFullPath($value)
        return $full.TrimEnd('\', '/')
    } catch {
        return $value.TrimEnd('\', '/')
    }
}

function Test-JiaSamePath {
    param(
        [string]$Left,
        [string]$Right
    )
    $a = Get-JiaNormalizedPath $Left
    $b = Get-JiaNormalizedPath $Right
    if ([string]::IsNullOrEmpty($a) -or [string]::IsNullOrEmpty($b)) {
        return $false
    }
    return [string]::Equals($a, $b, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-JiaAllowedArguments {
    param(
        [string]$Arguments,
        [string[]]$AllowedArguments
    )
    if ($null -eq $AllowedArguments) {
        return $false
    }
    foreach ($allowed in $AllowedArguments) {
        if ([string]::Equals($Arguments, $allowed, [System.StringComparison]::Ordinal)) {
            return $true
        }
    }
    return $false
}

function Get-JiaTaskOwnership {
    param(
        $Info,
        [string]$ExpectedExecute,
        [string]$ExpectedRoot,
        [string[]]$AllowedArguments
    )
    if ([string]::IsNullOrWhiteSpace($ExpectedExecute) -or
        [string]::IsNullOrWhiteSpace($ExpectedRoot) -or
        $null -eq $AllowedArguments -or
        $AllowedArguments.Count -eq 0) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Invalid' -Message 'Expected ownership data is incomplete.'
    }
    if ($null -eq $Info -or $null -eq $Info.PSObject.Properties['Exists']) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Unreadable' -Message 'The scheduled task info is missing.'
    }
    if (-not $Info.Readable) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Unreadable' -Message 'The scheduled task could not be read.'
    }
    if (-not $Info.Exists) {
        return New-JiaOwnershipDecision -Allowed $true -State 'Absent' -Message 'No scheduled task with this name exists.'
    }
    if ([int]$Info.ActionCount -ne 1) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Foreign' -Message 'The task does not have exactly one action.'
    }
    if ([int]$Info.ActionType -ne 0) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Foreign' -Message 'The task action is not an executable launch.'
    }
    if (-not (Test-JiaSamePath $Info.Execute $ExpectedExecute)) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Foreign' -Message 'The task runs a different executable.'
    }
    if (-not (Test-JiaAllowedArguments $Info.Arguments $AllowedArguments)) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Foreign' -Message 'The task has different arguments.'
    }
    if (-not (Test-JiaSamePath $Info.WorkingDirectory $ExpectedRoot)) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Foreign' -Message 'The task has a different working directory.'
    }
    return New-JiaOwnershipDecision -Allowed $true -State 'Owned' -Message 'The task belongs to this JiaAgent project.'
}

function Get-JiaShortcutOwnership {
    param(
        $Info,
        [string]$ExpectedTarget,
        [string]$ExpectedRoot,
        [string[]]$AllowedArguments
    )
    if ([string]::IsNullOrWhiteSpace($ExpectedTarget) -or
        [string]::IsNullOrWhiteSpace($ExpectedRoot) -or
        $null -eq $AllowedArguments -or
        $AllowedArguments.Count -eq 0) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Invalid' -Message 'Expected ownership data is incomplete.'
    }
    if ($null -eq $Info -or $null -eq $Info.PSObject.Properties['Exists']) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Unreadable' -Message 'The shortcut info is missing.'
    }
    if (-not $Info.Readable) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Unreadable' -Message 'The shortcut properties could not be read.'
    }
    if (-not $Info.Exists) {
        return New-JiaOwnershipDecision -Allowed $true -State 'Absent' -Message 'No shortcut with this name exists.'
    }
    if (-not (Test-JiaSamePath $Info.TargetPath $ExpectedTarget)) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Foreign' -Message 'The shortcut targets a different executable.'
    }
    if (-not (Test-JiaAllowedArguments $Info.Arguments $AllowedArguments)) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Foreign' -Message 'The shortcut has different arguments.'
    }
    if (-not (Test-JiaSamePath $Info.WorkingDirectory $ExpectedRoot)) {
        return New-JiaOwnershipDecision -Allowed $false -State 'Foreign' -Message 'The shortcut has a different working directory.'
    }
    return New-JiaOwnershipDecision -Allowed $true -State 'Owned' -Message 'The shortcut belongs to this JiaAgent project.'
}

function Get-JiaExceptionHResult {
    param($ErrorRecord)
    $fallback = 0
    try {
        $exception = $ErrorRecord.Exception
    } catch {
        return 0
    }
    for ($i = 0; $i -lt 4 -and $null -ne $exception; $i++) {
        try {
            $hr = [int]$exception.HResult
        } catch {
            $hr = 0
        }
        if ($hr -eq $script:JiaTaskMissingHResult) {
            return $hr
        }
        if ($fallback -eq 0 -and $hr -ne 0) {
            $fallback = $hr
        }
        try {
            $exception = $exception.InnerException
        } catch {
            $exception = $null
        }
    }
    return $fallback
}

function New-JiaTaskInfo {
    return [pscustomobject]@{
        Exists = $false
        Readable = $true
        Execute = ''
        Arguments = ''
        WorkingDirectory = ''
        ActionCount = 0
        ActionType = -1
        Error = ''
    }
}

function New-JiaShortcutInfo {
    return [pscustomobject]@{
        Exists = $false
        Readable = $true
        TargetPath = ''
        Arguments = ''
        WorkingDirectory = ''
        Error = ''
    }
}

function Read-JiaScheduledTaskInfo {
    param([string]$TaskName)
    $info = New-JiaTaskInfo
    if ([string]::IsNullOrWhiteSpace($TaskName)) {
        $info.Readable = $false
        $info.Error = 'task-name-empty'
        return $info
    }
    try {
        $service = New-Object -ComObject 'Schedule.Service'
        $service.Connect()
        $folder = $service.GetFolder('\')
        $task = $folder.GetTask($TaskName)
    } catch {
        if ((Get-JiaExceptionHResult $_) -eq $script:JiaTaskMissingHResult) {
            return $info
        }
        $info.Exists = $true
        $info.Readable = $false
        $info.Error = 'task-query-failed'
        return $info
    }

    $info.Exists = $true
    try {
        $definition = $task.Definition
        $actions = @($definition.Actions)
        $info.ActionCount = $actions.Count
        if ($actions.Count -eq 1) {
            $action = $actions[0]
            $info.ActionType = [int]$action.Type
            if ([int]$action.Type -eq 0) {
                $info.Execute = [string]$action.Path
                $info.Arguments = [string]$action.Arguments
                $info.WorkingDirectory = [string]$action.WorkingDirectory
            }
        }
    } catch {
        $info.Readable = $false
        $info.Error = 'task-definition-unreadable'
    }
    return $info
}

function Read-JiaShortcutInfo {
    param([string]$Path)
    $info = New-JiaShortcutInfo
    if ([string]::IsNullOrWhiteSpace($Path)) {
        $info.Readable = $false
        $info.Error = 'shortcut-path-empty'
        return $info
    }
    try {
        $null = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        return $info
    } catch [System.IO.FileNotFoundException] {
        return $info
    } catch [System.IO.DirectoryNotFoundException] {
        return $info
    } catch {
        $info.Exists = $true
        $info.Readable = $false
        $info.Error = 'shortcut-query-failed'
        return $info
    }

    $info.Exists = $true
    try {
        $shell = New-Object -ComObject 'WScript.Shell'
        $shortcut = $shell.CreateShortcut($Path)
        $target = [string]$shortcut.TargetPath
        $info.TargetPath = $target
        $info.Arguments = [string]$shortcut.Arguments
        $info.WorkingDirectory = [string]$shortcut.WorkingDirectory
        if ([string]::IsNullOrWhiteSpace($target)) {
            $info.Readable = $false
            $info.Error = 'shortcut-target-unreadable'
        }
    } catch {
        $info.Readable = $false
        $info.Error = 'shortcut-properties-unreadable'
    }
    return $info
}

function Invoke-JiaTaskInstall {
    param(
        $Info,
        [string]$ExpectedExecute,
        [string]$ExpectedRoot,
        [string[]]$AllowedArguments,
        [scriptblock]$CreateOrUpdate
    )
    $decision = Get-JiaTaskOwnership -Info $Info -ExpectedExecute $ExpectedExecute -ExpectedRoot $ExpectedRoot -AllowedArguments $AllowedArguments
    if (-not $decision.Allowed) {
        return New-JiaOwnershipActionResult -Allowed $false -Changed $false -State $decision.State -Message $decision.Message
    }
    if ($null -eq $CreateOrUpdate) {
        return New-JiaOwnershipActionResult -Allowed $false -Changed $false -State 'Invalid' -Message 'No install callback was provided.'
    }
    try {
        & $CreateOrUpdate
    } catch {
        return New-JiaOwnershipActionResult -Allowed $false -Changed $false -State 'Error' -Message $_.Exception.Message
    }
    return New-JiaOwnershipActionResult -Allowed $true -Changed $true -State $decision.State -Message $decision.Message
}

function Invoke-JiaTaskUninstall {
    param(
        $Info,
        [string]$ExpectedExecute,
        [string]$ExpectedRoot,
        [string[]]$AllowedArguments,
        [scriptblock]$Remove
    )
    $decision = Get-JiaTaskOwnership -Info $Info -ExpectedExecute $ExpectedExecute -ExpectedRoot $ExpectedRoot -AllowedArguments $AllowedArguments
    if (-not $decision.Allowed) {
        return New-JiaOwnershipActionResult -Allowed $false -Changed $false -State $decision.State -Message $decision.Message
    }
    if ($decision.State -eq 'Absent') {
        return New-JiaOwnershipActionResult -Allowed $true -Changed $false -State 'Absent' -Message $decision.Message
    }
    if ($null -eq $Remove) {
        return New-JiaOwnershipActionResult -Allowed $false -Changed $false -State 'Invalid' -Message 'No remove callback was provided.'
    }
    try {
        & $Remove
    } catch {
        return New-JiaOwnershipActionResult -Allowed $false -Changed $false -State 'Error' -Message $_.Exception.Message
    }
    return New-JiaOwnershipActionResult -Allowed $true -Changed $true -State 'Owned' -Message $decision.Message
}

function Invoke-JiaShortcutInstall {
    param(
        $Info,
        [string]$ExpectedTarget,
        [string]$ExpectedRoot,
        [string[]]$AllowedArguments,
        [scriptblock]$CreateOrUpdate
    )
    $decision = Get-JiaShortcutOwnership -Info $Info -ExpectedTarget $ExpectedTarget -ExpectedRoot $ExpectedRoot -AllowedArguments $AllowedArguments
    if (-not $decision.Allowed) {
        return New-JiaOwnershipActionResult -Allowed $false -Changed $false -State $decision.State -Message $decision.Message
    }
    if ($null -eq $CreateOrUpdate) {
        return New-JiaOwnershipActionResult -Allowed $false -Changed $false -State 'Invalid' -Message 'No install callback was provided.'
    }
    try {
        & $CreateOrUpdate
    } catch {
        return New-JiaOwnershipActionResult -Allowed $false -Changed $false -State 'Error' -Message $_.Exception.Message
    }
    return New-JiaOwnershipActionResult -Allowed $true -Changed $true -State $decision.State -Message $decision.Message
}

function Invoke-JiaShortcutUninstall {
    param(
        $Info,
        [string]$ExpectedTarget,
        [string]$ExpectedRoot,
        [string[]]$AllowedArguments,
        [scriptblock]$Remove
    )
    $decision = Get-JiaShortcutOwnership -Info $Info -ExpectedTarget $ExpectedTarget -ExpectedRoot $ExpectedRoot -AllowedArguments $AllowedArguments
    if (-not $decision.Allowed) {
        return New-JiaOwnershipActionResult -Allowed $false -Changed $false -State $decision.State -Message $decision.Message
    }
    if ($decision.State -eq 'Absent') {
        return New-JiaOwnershipActionResult -Allowed $true -Changed $false -State 'Absent' -Message $decision.Message
    }
    if ($null -eq $Remove) {
        return New-JiaOwnershipActionResult -Allowed $false -Changed $false -State 'Invalid' -Message 'No remove callback was provided.'
    }
    try {
        & $Remove
    } catch {
        return New-JiaOwnershipActionResult -Allowed $false -Changed $false -State 'Error' -Message $_.Exception.Message
    }
    return New-JiaOwnershipActionResult -Allowed $true -Changed $true -State 'Owned' -Message $decision.Message
}
