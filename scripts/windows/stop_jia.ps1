# Stop the JiaAgent instance recorded in data\jiaagent.pid.
#
# Order:
#   1. Read the JSON state file (pid + process start time + event name).
#   2. Verify the PID still belongs to THIS project's interpreter running
#      THIS project's run.py, and that its start time matches. Fail closed.
#   3. Signal the per-user named event; uvicorn then runs the normal lifespan
#      cleanup (WeChat supervisor, window-focus watcher, databases).
#   4. If it is still there after the grace period, force-stop ONLY the
#      verified main process. There is no /T and no parent/child traversal:
#      applications launched by Jia (or the redacting launcher wrapper) are
#      not killed by association.
#
# -Force skips the grace period but still verifies identity first. It never
# broadens the target beyond the one verified main PID.
#
# If the process exists but its identity cannot be verified (PID reused,
# CIM/access error), this script refuses to kill it and exits 1. It also
# leaves the state file in place for inspection instead of pretending success.
#
# A missing state file is NOT treated as proof that Jia is stopped. The script
# runs the strict health check and a read-only scan for a process matching this
# project's interpreter + run.py. It exits 1 with an explanation unless every
# check that could run completed and found no JiaAgent. A corrupt or unreadable
# state file also exits 1 and is never deleted automatically.
[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'jia_common.ps1')

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$PidPath = Join-Path $Root 'data\jiaagent.pid'
$expectedPython = Join-Path $Root '.venv\Scripts\python.exe'
$expectedScript = Join-Path $Root 'run.py'

function Remove-OwnedState {
    [void](Remove-JiaPidStateIfOwned `
        -Path $PidPath -PidValue $state.Pid -StartedAt $state.StartedAt)
}

# Returns Gone / Verified / Unverified. Never returns anything else.
function Get-JiaTargetState {
    if (-not (Test-JiaPidExists -PidValue $state.Pid)) { return 'Gone' }
    $info = Get-JiaProcessInfo -PidValue $state.Pid
    if ($null -eq $info) { return 'Unverified' }
    if (Test-JiaProcessIdentity `
        -Info $info `
        -ExpectedPython $expectedPython `
        -ExpectedScript $expectedScript `
        -ExpectedStartTime $state.StartedAt) {
        return 'Verified'
    }
    return 'Unverified'
}

$stateResult = Read-JiaPidStateResult -Path $PidPath
if ($stateResult.State -eq 'Corrupt' -or $stateResult.State -eq 'Unreadable') {
    Write-Host "ERROR: PID state file '$PidPath' is $($stateResult.State.ToLowerInvariant()) ($($stateResult.Reason))."
    Write-Host 'The running state cannot be confirmed safely.'
    Write-Host 'Refusing to guess a PID, stop an unknown process, or delete the state file.'
    Write-Host 'Run status_jia.ps1 and check /api/health before acting manually.'
    exit 1
}

if ($stateResult.State -eq 'Missing') {
    # Nothing recorded a PID. A missing file is not proof that Jia is stopped:
    # check the strict health endpoint first, then look for a process that
    # matches this project's interpreter and run.py. Only a completed scan with
    # no match is enough evidence to exit 0.
    $endpoint = Resolve-JiaEndpoint -Root $Root
    $healthChecked = $false
    $healthOk = $false
    $healthError = ''
    if ($endpoint.Ok) {
        $health = Get-JiaHealth -BaseUrl $endpoint.BaseUrl
        if ($health.Checked) {
            $healthChecked = $true
            $healthOk = [bool]$health.Ok
            if (-not $healthOk) {
                $healthError = 'health endpoint did not return a valid JiaAgent response'
            }
        } else {
            # Any non-empty probe Error means the check did not reliably run.
            # This must not be treated as "service unreachable".
            $healthError = 'health check could not be executed: ' + [string]$health.Error
        }
    } else {
        $healthError = [string]$endpoint.Reason
    }

    $scan = Find-JiaProcess -ExpectedPython $expectedPython -ExpectedScript $expectedScript
    $outcome = Get-JiaNoPidStopOutcome `
        -HealthChecked $healthChecked `
        -HealthOk $healthOk `
        -HealthError $healthError `
        -ScanOk ([bool]$scan.Ok) `
        -JiaPids @($scan.Pids) `
        -ScanReason ([string]$scan.Reason)
    Write-Host $outcome.Message
    exit ([int]$outcome.ExitCode)
}

$state = $stateResult.Value

if ($state.Legacy) {
    if (-not (Test-JiaPidExists -PidValue $state.Pid)) {
        Write-Host "PID $($state.Pid) is not running (stale legacy state file); nothing to stop."
        exit 0
    }
    Write-Host 'The PID file is from an older version and has no start time.'
    Write-Host 'Refusing to kill a PID that cannot be verified against PID reuse.'
    Write-Host "Start JiaAgent once with the current version, or remove $PidPath manually after checking it."
    exit 1
}

if ($null -eq $state.StartedAt) {
    if (-not (Test-JiaPidExists -PidValue $state.Pid)) {
        [void](Remove-JiaPidStateIfOwned -Path $PidPath -PidValue $state.Pid)
        Write-Host "PID $($state.Pid) is not running; removed the stale state file."
        exit 0
    }
    Write-Host 'The PID state file has no process start time; refusing to kill it.'
    exit 1
}

$targetState = Get-JiaTargetState
if ($targetState -eq 'Gone') {
    [void](Remove-JiaPidStateIfOwned `
        -Path $PidPath -PidValue $state.Pid -StartedAt $state.StartedAt)
    Write-Host "PID $($state.Pid) is not running (stale state file); nothing to stop."
    exit 0
}
if ($targetState -ne 'Verified') {
    Write-Host "PID $($state.Pid) is running but does not match this JiaAgent instance."
    Write-Host 'Refusing to kill a PID whose identity cannot be verified.'
    exit 1
}

if (-not $Force) {
    $eventName = [string]$state.ShutdownEvent
    $signalled = $false
    if ($eventName -match '^Local\\JiaAgent-Shutdown-[0-9a-f]{12}$') {
        try {
            $evt = [System.Threading.EventWaitHandle]::OpenExisting($eventName)
            try {
                [void]$evt.Set()
                $signalled = $true
                Write-Host 'Sent graceful shutdown request; waiting for cleanup...'
            } finally {
                $evt.Dispose()
            }
        } catch {
            Write-Host 'Graceful shutdown event is unavailable; will verify again before forcing.'
        }
    } else {
        Write-Host 'No valid shutdown event in the PID state file.'
    }

    if ($signalled) {
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-Date) -lt $deadline) {
            $targetState = Get-JiaTargetState
            if ($targetState -eq 'Gone') {
                Remove-OwnedState
                Write-Host 'JiaAgent stopped gracefully.'
                exit 0
            }
            if ($targetState -eq 'Unverified') {
                Write-Host "PID $($state.Pid) is still running but can no longer be verified."
                Write-Host 'Refusing to force-stop it; leaving the state file for inspection.'
                exit 1
            }
            Start-Sleep -Milliseconds 300
        }
        Write-Host 'Graceful shutdown did not finish in 15 seconds.'
    }
}

# Re-verify immediately before the only destructive operation. If the PID
# disappeared or was reused, do not touch it.
$targetState = Get-JiaTargetState
if ($targetState -eq 'Gone') {
    Remove-OwnedState
    Write-Host 'The original JiaAgent process is already gone; nothing was killed.'
    exit 0
}
if ($targetState -ne 'Verified') {
    Write-Host "PID $($state.Pid) is running but does not match this JiaAgent instance."
    Write-Host 'Refusing to force-stop a process whose identity cannot be verified.'
    exit 1
}

Write-Host "Force-stopping the verified main process only (PID $($state.Pid), no child tree)..."
Stop-Process -Id $state.Pid -Force -ErrorAction SilentlyContinue

$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    $targetState = Get-JiaTargetState
    if ($targetState -eq 'Gone') {
        Remove-OwnedState
        Write-Host 'JiaAgent stopped (main process only).'
        exit 0
    }
    if ($targetState -eq 'Unverified') {
        Write-Host "PID $($state.Pid) still exists but can no longer be verified; leaving the state file."
        exit 1
    }
    Start-Sleep -Milliseconds 300
}

Write-Host "ERROR: PID $($state.Pid) is still running."
exit 1
