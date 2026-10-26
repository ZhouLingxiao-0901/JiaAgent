# Read-only status report for JiaAgent.
#
# Prints: scheduled-task/startup registration, verified PID state, health
# endpoint, and the local WeChat runtime status file. It never prints .env
# contents, tokens, context tokens, or message bodies.
#
# A health check succeeds only for HTTP 200 + a JiaAgent body
# (status=ok, name=JiaAgent, semantic version). Any JSON from another local
# service is not reported as Jia.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'

. (Join-Path $PSScriptRoot 'jia_common.ps1')

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$TaskName = 'JiaAgent'
$PidPath = Join-Path $Root 'data\jiaagent.pid'
$WeChatStatusPath = Join-Path $Root 'data\wechat\runtime-status.json'

Write-Host '=== JiaAgent status ==='

# ---- auto-start registration ----
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    Write-Host ("Scheduled task : {0} ({1})" -f $TaskName, $task.State)
    if ($null -ne $info) {
        Write-Host ("  last run     : {0}" -f $info.LastRunTime)
        Write-Host ("  last result  : {0}" -f $info.LastTaskResult)
    }
} else {
    Write-Host ("Scheduled task : {0} is not installed" -f $TaskName)
}

$shortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'JiaAgent.lnk'
if (Test-Path -LiteralPath $shortcutPath) {
    Write-Host ("Startup shortcut: {0}" -f $shortcutPath)
}

# ---- process identity ----
$pidStateResult = Read-JiaPidStateResult -Path $PidPath
$pidState = $pidStateResult.Value
$basicIdentity = $false
$verifiedIdentity = $false

if ($pidStateResult.State -eq 'Missing') {
    Write-Host 'Process        : no PID state file'
} elseif ($pidStateResult.State -eq 'Corrupt') {
    Write-Host ("Process        : PID state file is malformed ({0})" -f $pidStateResult.Reason)
} elseif ($pidStateResult.State -eq 'Unreadable') {
    Write-Host ("Process        : PID state file exists but cannot be read ({0})" -f $pidStateResult.Reason)
} elseif ($pidState.Legacy) {
    $alive = Test-JiaPidExists -PidValue $pidState.Pid
    if (-not $alive) {
        Write-Host ("Process        : stale legacy PID file ({0} is not running)" -f $pidState.Pid)
    } else {
        Write-Host ("Process        : PID {0} is alive, but the legacy file has no start time; identity unverified" -f $pidState.Pid)
    }
} else {
    $info = Get-JiaProcessInfo -PidValue $pidState.Pid
    if ($null -eq $info) {
        if (Test-JiaPidExists -PidValue $pidState.Pid) {
            Write-Host ("Process        : PID {0} is alive but identity could not be read" -f $pidState.Pid)
        } else {
            Write-Host ("Process        : stale PID state file ({0} is not running)" -f $pidState.Pid)
        }
    } else {
        $expectedPython = Join-Path $Root '.venv\Scripts\python.exe'
        $expectedScript = Join-Path $Root 'run.py'
        $basicIdentity = Test-JiaProcessIdentity `
            -Info $info `
            -ExpectedPython $expectedPython `
            -ExpectedScript $expectedScript `
            -ExpectedStartTime $null
        if (-not $basicIdentity) {
            Write-Host ("Process        : PID {0} is alive but does not match this project's python/run.py" -f $pidState.Pid)
        } elseif ($null -eq $pidState.StartedAt) {
            Write-Host ("Process        : running (PID {0}); start time missing, identity partially verified" -f $pidState.Pid)
        } else {
            $verifiedIdentity = Test-JiaProcessIdentity `
                -Info $info `
                -ExpectedPython $expectedPython `
                -ExpectedScript $expectedScript `
                -ExpectedStartTime $pidState.StartedAt
            if ($verifiedIdentity) {
                Write-Host ("Process        : running (PID {0}, identity verified)" -f $pidState.Pid)
            } else {
                Write-Host ("Process        : PID {0} was reused by another process; not JiaAgent" -f $pidState.Pid)
            }
        }
    }
}

# ---- health ----
$healthy = $false
# Only trust the running instance's recorded host/port when its start time
# matches too; otherwise fall back to .env instead of probing an arbitrary
# process that happens to have inherited the same PID.
$endpoint = Resolve-JiaEndpoint -Root $Root -PidState $(if ($verifiedIdentity) { $pidState } else { $null })
if (-not $endpoint.Ok) {
    Write-Host ("Health         : not checked ({0})" -f $endpoint.Reason)
} else {
    $health = Get-JiaHealth -BaseUrl $endpoint.BaseUrl
    if (-not $health.Checked) {
        Write-Host ("Health         : not checked (health probe could not run: {0})" -f $health.Error)
    } elseif ($health.Ok) {
        $healthy = $true
        Write-Host ("Health         : OK (JiaAgent v{0}) at {1}/api/health" -f $health.Data.version, $endpoint.BaseUrl)
    } else {
        Write-Host ("Health         : NOT a valid JiaAgent health response at {0}/api/health" -f $endpoint.BaseUrl)
        if ($health.StatusCode -gt 0) {
            Write-Host ("  HTTP status  : {0}" -f $health.StatusCode)
        }
        if ($null -ne $health.Data) {
            Write-Host ("  response     : name={0} status={1} version={2}" -f `
                $health.Data.name, $health.Data.status, $health.Data.version)
        }
    }
}

# ---- WeChat listener (no credentials are stored in this file) ----
if (Test-Path -LiteralPath $WeChatStatusPath) {
    try {
        $wechatState = Get-Content -LiteralPath $WeChatStatusPath -Raw | ConvertFrom-Json
        $recordedPid = 0
        if ($null -ne $wechatState.pid -and "$($wechatState.pid)" -match '^\d+$') {
            $recordedPid = [int]$wechatState.pid
        }

        # A file left by a previous run must not be reported as live. The
        # Python side uses the same fail-closed rule (runtime_state.py).
        $runtimeAlive = $false
        if ($recordedPid -gt 0) {
            $runtimeAlive = Test-JiaPidExists -PidValue $recordedPid
        } elseif ($wechatState.state -eq 'unknown') {
            $runtimeAlive = $true
        }

        if ($wechatState.state -eq 'stopped') {
            Write-Host ("WeChat         : stopped (recorded by PID {0})" -f $recordedPid)
        } elseif ($recordedPid -le 0) {
            Write-Host ("WeChat         : state={0} (PID missing; liveness unknown, treat as possibly running)" -f $wechatState.state)
        } elseif (-not $runtimeAlive) {
            Write-Host ("WeChat         : stale status file; recorded process {0} is not running" -f $recordedPid)
            Write-Host ("  last state   : {0}" -f $wechatState.state)
        } else {
            Write-Host ("WeChat         : state={0} monitor_alive={1} restarts={2}" -f `
                $wechatState.state, $wechatState.monitor_alive, $wechatState.restart_count)
        }
        if ($wechatState.last_error) {
            Write-Host ("  last error   : {0}" -f $wechatState.last_error)
        }
    } catch {
        Write-Host 'WeChat         : status file exists but could not be parsed'
    }
} else {
    Write-Host 'WeChat         : no runtime status file (listener not started)'
}

if ($healthy) { exit 0 }
exit 1
