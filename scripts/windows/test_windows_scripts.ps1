# Offline safety tests for the Windows launcher/status/stop scripts.
#
# No real JiaAgent process is started or stopped. Process identity and health
# validation are tested with mock objects and injected fetch responses; the
# only real network call is to a closed loopback port.
#
# This file is intentionally ASCII-only: Windows PowerShell 5.1 reads a
# UTF-8 file without BOM using the ANSI code page.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'jia_common.ps1')

$script:passed = 0
$script:failed = 0

function Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = '')
    if ($Ok) {
        $script:passed++
        Write-Host ("  [OK] " + $Name)
    } else {
        $script:failed++
        Write-Host ("  [!!] " + $Name + $(if ($Detail) { " : " + $Detail } else { "" }))
    }
}

function Test-Throws {
    param([scriptblock]$Block)
    try {
        & $Block | Out-Null
        return $false
    } catch {
        return $true
    }
}

function Get-FreeLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}

Write-Host 'Windows script safety tests (no service started, no real process stopped)'

Write-Host 'Health payload validation:'
$goodBody = '{"status":"ok","name":"JiaAgent","version":"0.1.0"}'
Check 'accept Jia 200 health response' (Test-JiaHealthPayload -StatusCode 200 -Body $goodBody -ContentType 'application/json')
Check 'reject HTTP 500 even with valid JSON' (-not (Test-JiaHealthPayload -StatusCode 500 -Body $goodBody -ContentType 'application/json'))
Check 'reject another service JSON' (-not (Test-JiaHealthPayload -StatusCode 200 -Body '{"status":"ok","name":"Other","version":"1.0.0"}' -ContentType 'application/json'))
Check 'reject wrong status value' (-not (Test-JiaHealthPayload -StatusCode 200 -Body '{"status":"bad","name":"JiaAgent","version":"0.1.0"}' -ContentType 'application/json'))
Check 'reject missing version' (-not (Test-JiaHealthPayload -StatusCode 200 -Body '{"status":"ok","name":"JiaAgent"}' -ContentType 'application/json'))
Check 'reject broken JSON' (-not (Test-JiaHealthPayload -StatusCode 200 -Body 'not-json' -ContentType 'application/json'))
Check 'reject wrong Content-Type' (-not (Test-JiaHealthPayload -StatusCode 200 -Body $goodBody -ContentType 'text/html'))
Check 'accept empty Content-Type (curl may omit)' (Test-JiaHealthPayload -StatusCode 200 -Body $goodBody)

$validFetch = { param($Target) [pscustomobject]@{ StatusCode = 200; Body = $goodBody; ContentType = 'application/json'; Error = '' } }
$otherFetch = { param($Target) [pscustomobject]@{ StatusCode = 200; Body = '{"status":"ok","name":"Other","version":"1.0.0"}'; ContentType = 'application/json'; Error = '' } }
$validProbe = Get-JiaHealth -BaseUrl 'http://127.0.0.1:8000' -Fetch $validFetch
Check 'Get-JiaHealth accepts valid response' ($validProbe.Ok)
Check 'valid health response is marked as reliably checked' ([bool]$validProbe.Checked)
$otherProbe = Get-JiaHealth -BaseUrl 'http://127.0.0.1:8000' -Fetch $otherFetch
Check 'Get-JiaHealth rejects other service JSON' (-not $otherProbe.Ok)
Check 'a wrong service response is still a completed health check' ([bool]$otherProbe.Checked)
$closedPort = Get-FreeLoopbackPort
$downProbe = Get-JiaHealth -BaseUrl ("http://127.0.0.1:" + $closedPort)
Check 'health check fails when service is down' (-not $downProbe.Ok)
Check 'network unreachable is a completed health check' ($downProbe.Checked -and [string]::IsNullOrWhiteSpace([string]$downProbe.Error)) (("checked={0} status={1} error=[{2}]" -f $downProbe.Checked, $downProbe.StatusCode, $downProbe.Error))
$errorFetch = { param($Target) [pscustomobject]@{ StatusCode = 0; Body = ''; ContentType = ''; Error = 'IOException' } }
$errorProbe = Get-JiaHealth -BaseUrl 'http://127.0.0.1:8000' -Fetch $errorFetch
Check 'non-empty health error is not a completed check' ($null -ne $errorProbe.PSObject.Properties['Checked'] -and (-not $errorProbe.Checked) -and $errorProbe.Error -eq 'IOException')
$okWithErrorFetch = { param($Target) [pscustomobject]@{ StatusCode = 200; Body = $goodBody; ContentType = 'application/json'; Error = 'probe warning' } }
$okWithErrorProbe = Get-JiaHealth -BaseUrl 'http://127.0.0.1:8000' -Fetch $okWithErrorFetch
Check 'a probe error invalidates an otherwise valid body' ((-not $okWithErrorProbe.Checked) -and (-not $okWithErrorProbe.Ok))
$throwFetch = { param($Target) throw 'injected health probe failure' }
$throwProbe = Get-JiaHealth -BaseUrl 'http://127.0.0.1:8000' -Fetch $throwFetch
Check 'health probe exception is not a completed check' ($null -ne $throwProbe.PSObject.Properties['Checked'] -and (-not $throwProbe.Checked) -and -not [string]::IsNullOrWhiteSpace([string]$throwProbe.Error))
$tempBlocker = Join-Path $env:TEMP ('jia-temp-block-' + [guid]::NewGuid().ToString('N'))
Set-Content -LiteralPath $tempBlocker -Value 'not a directory' -Encoding ascii
$savedTemp = $env:TEMP
$savedTmp = $env:TMP
try {
    $env:TEMP = $tempBlocker
    $env:TMP = $tempBlocker
    $tempProbe = Get-JiaHealth -BaseUrl ("http://127.0.0.1:" + $closedPort)
} finally {
    $env:TEMP = $savedTemp
    $env:TMP = $savedTmp
    Remove-Item -LiteralPath $tempBlocker -Force -ErrorAction SilentlyContinue
}
Check 'temporary-file creation failure is not a completed check' ($null -ne $tempProbe.PSObject.Properties['Checked'] -and (-not $tempProbe.Checked) -and -not [string]::IsNullOrWhiteSpace([string]$tempProbe.Error))

Write-Host 'Address and IPv4/IPv6:'
Check 'IPv4 loopback URL' ((Format-JiaBaseUrl '127.0.0.1' 8000) -eq 'http://127.0.0.1:8000')
Check 'localhost URL' ((Format-JiaBaseUrl 'localhost' 8000) -eq 'http://localhost:8000')
Check 'IPv6 loopback URL gets brackets' ((Format-JiaBaseUrl '::1' 8000) -eq 'http://[::1]:8000')
Check 'bracketed IPv6 is not double wrapped' ((Format-JiaBaseUrl '[::1]' 8000) -eq 'http://[::1]:8000')
Check 'reject 0.0.0.0' (Test-Throws { Format-JiaBaseUrl '0.0.0.0' 8000 })
Check 'reject LAN address' (Test-Throws { Format-JiaBaseUrl '192.168.1.10' 8000 })
Check 'reject non-loopback name' (Test-Throws { Format-JiaBaseUrl 'evil.example' 8000 })

$tmp = Join-Path $env:TEMP ('jia-common-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    Set-Content -LiteralPath (Join-Path $tmp '.env') -Value "JIA_HOST=::1`nJIA_PORT=8123" -Encoding utf8
    $savedHost = $env:JIA_HOST
    $savedPort = $env:JIA_PORT
    try {
        $env:JIA_HOST = $null
        $env:JIA_PORT = $null
        $endpoint = Resolve-JiaEndpoint -Root $tmp
        Check '.env IPv6 address is used for health checks' ($endpoint.Ok -and $endpoint.BaseUrl -eq 'http://[::1]:8123') $endpoint.Reason
    } finally {
        $env:JIA_HOST = $savedHost
        $env:JIA_PORT = $savedPort
    }

    Set-Content -LiteralPath (Join-Path $tmp '.env') -Value "JIA_HOST=0.0.0.0`nJIA_PORT=8123" -Encoding utf8
    Check '.env non-loopback address is rejected' (-not (Resolve-JiaEndpoint -Root $tmp).Ok)

    Set-Content -LiteralPath (Join-Path $tmp '.env') -Value "JIA_HOST=127.0.0.1`nJIA_PORT=not-a-port" -Encoding utf8
    Check '.env invalid port is rejected' (-not (Resolve-JiaEndpoint -Root $tmp).Ok)

    Set-Content -LiteralPath (Join-Path $tmp '.env') -Value "JIA_HOST=127.0.0.1`nJIA_PORT=8000" -Encoding utf8
    $pidEndpoint = Resolve-JiaEndpoint -Root $tmp -PidState ([pscustomobject]@{ Legacy = $false; Host = '127.0.0.1'; Port = 9001 })
    Check 'running instance PID state wins over .env' ($pidEndpoint.Ok -and $pidEndpoint.BaseUrl -eq 'http://127.0.0.1:9001')
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host 'Process identity:'
$expectedExe = 'D:\JiaAgent\.venv\Scripts\python.exe'
$expectedScript = 'D:\JiaAgent\run.py'
$now = [DateTime]::UtcNow
$started = ([DateTimeOffset]$now).ToUnixTimeSeconds()
$command = '"' + $expectedExe + '" "' + $expectedScript + '"'
$goodInfo = [pscustomobject]@{
    Pid = 1234
    ExecutablePath = $expectedExe
    CommandLine = $command
    StartTimeUtc = $now
}
Check 'accept project python + exact run.py argument' (Test-JiaProcessIdentity -Info $goodInfo -ExpectedPython $expectedExe -ExpectedScript $expectedScript -ExpectedStartTime $started)
Check 'accept unquoted exact script argument' (Test-JiaScriptArgument -CommandLine ($expectedExe + ' ' + $expectedScript) -ScriptPath $expectedScript)
Check 'accept forward-slash script path' (Test-JiaScriptArgument -CommandLine ($expectedExe + ' D:/JiaAgent/run.py') -ScriptPath $expectedScript)
Check 'reject run.py.bak substring' (-not (Test-JiaScriptArgument -CommandLine ($expectedExe + ' "' + $expectedScript + '.bak"') -ScriptPath $expectedScript))
Check 'reject run.py in another directory' (-not (Test-JiaScriptArgument -CommandLine ($expectedExe + ' "C:\Other\run.py"') -ScriptPath $expectedScript))
Check 'reject letter-prefixed run.py substring' (-not (Test-JiaScriptArgument -CommandLine ($expectedExe + ' C:\Other\myrun.py') -ScriptPath $expectedScript))
Check 'reject empty command line' (-not (Test-JiaScriptArgument -CommandLine '' -ScriptPath $expectedScript))
Check 'reject wrong interpreter path' (-not (Test-JiaProcessIdentity -Info $goodInfo -ExpectedPython 'D:\Other\python.exe' -ExpectedScript $expectedScript -ExpectedStartTime $started))
Check 'reject wrong script path' (-not (Test-JiaProcessIdentity -Info $goodInfo -ExpectedPython $expectedExe -ExpectedScript 'D:\Other\run.py' -ExpectedStartTime $started))
Check 'reject start time mismatch (PID reuse)' (-not (Test-JiaProcessIdentity -Info $goodInfo -ExpectedPython $expectedExe -ExpectedScript $expectedScript -ExpectedStartTime ($started + 60)))
Check 'reject missing start time when required' (-not (Test-JiaProcessIdentity -Info ([pscustomobject]@{ Pid = 1234; ExecutablePath = $expectedExe; CommandLine = $command; StartTimeUtc = $null }) -ExpectedPython $expectedExe -ExpectedScript $expectedScript -ExpectedStartTime $started))

Write-Host 'PID existence helpers:'
Check 'current PID is detected' (Test-JiaPidExists -PidValue $PID)
Check 'PID 0 is not a process' (-not (Test-JiaPidExists -PidValue 0))
$selfInfo = Get-JiaProcessInfo -PidValue $PID
Check 'current process start time is readable' ($null -ne $selfInfo -and $null -ne $selfInfo.StartTimeUtc)
Check 'current process command line is readable' ($null -ne $selfInfo -and -not [string]::IsNullOrWhiteSpace($selfInfo.CommandLine))

Write-Host 'PID state file:'
$pidDir = Join-Path $env:TEMP ('jia-pid-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $pidDir | Out-Null
try {
    $pidPath = Join-Path $pidDir 'jiaagent.pid'
    $stateJson = '{"pid":4321,"started_at":100.5,"host":"127.0.0.1","port":8000,"shutdown_event":"Local\\JiaAgent-Shutdown-abcdef123456"}'
    Set-Content -LiteralPath $pidPath -Value $stateJson -Encoding ascii
    $state = Read-JiaPidState -Path $pidPath
    Check 'read JSON PID state' ($null -ne $state -and -not $state.Legacy -and $state.Pid -eq 4321 -and [math]::Abs($state.StartedAt - 100.5) -lt 0.001)
    Check 'JSON state keeps shutdown event name' ($state.ShutdownEvent -eq 'Local\JiaAgent-Shutdown-abcdef123456')
    Check 'remove state only on matching PID + start time' (Remove-JiaPidStateIfOwned -Path $pidPath -PidValue 4321 -StartedAt 100.5)
    Check 'state file is gone after removal' (-not (Test-Path -LiteralPath $pidPath))

    Set-Content -LiteralPath $pidPath -Value '4321' -Encoding ascii
    $legacy = Read-JiaPidState -Path $pidPath
    Check 'read legacy integer PID file' ($null -ne $legacy -and $legacy.Legacy -and $legacy.Pid -eq 4321)
    Check 'legacy state is not removed (no start time)' (-not (Remove-JiaPidStateIfOwned -Path $pidPath -PidValue 4321 -StartedAt 100.5))

    Set-Content -LiteralPath $pidPath -Value 'not-json' -Encoding ascii
    Check 'broken PID state is unusable' ($null -eq (Read-JiaPidState -Path $pidPath))
} finally {
    Remove-Item -LiteralPath $pidDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host 'PID state classification:'
$stateDir = Join-Path $env:TEMP ('jia-state-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $stateDir | Out-Null
try {
    $statePath = Join-Path $stateDir 'jiaagent.pid'

    $missingState = Read-JiaPidStateResult -Path $statePath
    Check 'missing PID state is reported as missing' ($missingState.State -eq 'Missing' -and $null -eq $missingState.Value)

    $validJson = '{"pid":4321,"started_at":100.5,"host":"127.0.0.1","port":8000,"shutdown_event":"Local\\JiaAgent-Shutdown-abcdef123456"}'
    Set-Content -LiteralPath $statePath -Value $validJson -Encoding ascii
    $validState = Read-JiaPidStateResult -Path $statePath
    Check 'valid PID state is reported as valid' ($validState.State -eq 'Valid' -and $null -ne $validState.Value -and $validState.Value.Pid -eq 4321)

    Set-Content -LiteralPath $statePath -Value '' -Encoding ascii
    $emptyState = Read-JiaPidStateResult -Path $statePath
    Check 'empty PID state is reported as corrupt' ($emptyState.State -eq 'Corrupt')

    Set-Content -LiteralPath $statePath -Value 'not-json' -Encoding ascii
    $brokenState = Read-JiaPidStateResult -Path $statePath
    Check 'broken JSON is reported as corrupt' ($brokenState.State -eq 'Corrupt' -and $null -eq (Read-JiaPidState -Path $statePath))

    Set-Content -LiteralPath $statePath -Value '{"pid":0}' -Encoding ascii
    $badPidState = Read-JiaPidStateResult -Path $statePath
    Check 'invalid PID is reported as corrupt' ($badPidState.State -eq 'Corrupt')

    Set-Content -LiteralPath $statePath -Value '{"pid":1234,"started_at":"not-a-number"}' -Encoding ascii
    $badStartedState = Read-JiaPidStateResult -Path $statePath
    Check 'invalid start time is reported as corrupt' ($badStartedState.State -eq 'Corrupt')

    Set-Content -LiteralPath $statePath -Value '4321' -Encoding ascii
    $legacyState = Read-JiaPidStateResult -Path $statePath
    Check 'legacy PID state remains valid' ($legacyState.State -eq 'Valid' -and $legacyState.Value.Legacy)

    Remove-Item -LiteralPath $statePath -Force
    New-Item -ItemType Directory -Path $statePath | Out-Null
    $unreadableState = Read-JiaPidStateResult -Path $statePath
    Check 'unreadable PID state is reported as unreadable' ($unreadableState.State -eq 'Unreadable')
} finally {
    Remove-Item -LiteralPath $stateDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host 'Process scan for untracked JiaAgent:'
$scanExe = 'D:\JiaAgent\.venv\Scripts\python.exe'
$scanScript = 'D:\JiaAgent\run.py'
$scanCommand = '"' + $scanExe + '" "' + $scanScript + '"'
$matchingProcesses = @(
    [pscustomobject]@{ ProcessId = 4321; ExecutablePath = $scanExe; CommandLine = $scanCommand },
    [pscustomobject]@{ ProcessId = 4322; ExecutablePath = 'C:\Other\python.exe'; CommandLine = 'C:\Other\python.exe other.py' }
)
$scan = Find-JiaProcess -ExpectedPython $scanExe -ExpectedScript $scanScript -GetProcesses { $matchingProcesses }
Check 'matching python + run.py is found' ($scan.Ok -and @($scan.Pids).Count -eq 1 -and @($scan.Pids) -contains 4321)

$otherProcesses = @(
    [pscustomobject]@{ ProcessId = 5000; ExecutablePath = 'C:\Other\python.exe'; CommandLine = 'python other.py' }
)
$scan = Find-JiaProcess -ExpectedPython $scanExe -ExpectedScript $scanScript -GetProcesses { $otherProcesses }
Check 'unrelated python processes are ignored' ($scan.Ok -and @($scan.Pids).Count -eq 0)

$exeOnly = @(
    [pscustomobject]@{ ProcessId = 5001; ExecutablePath = $scanExe; CommandLine = '' }
)
$scan = Find-JiaProcess -ExpectedPython $scanExe -ExpectedScript $scanScript -GetProcesses { $exeOnly }
Check 'expected python without a readable command line is unconfirmed' ((-not $scan.Ok) -and $scan.Reason -eq 'process-identity-unreadable')

$scan = Find-JiaProcess -ExpectedPython $scanExe -ExpectedScript $scanScript -GetProcesses { throw 'access denied' }
Check 'process enumeration failure is unconfirmed' ((-not $scan.Ok) -and $scan.Reason -eq 'process-query-failed')

$backupProcesses = @(
    [pscustomobject]@{ ProcessId = 5002; ExecutablePath = $scanExe; CommandLine = ('"' + $scanExe + '" "' + $scanScript + '.bak"') }
)
$scan = Find-JiaProcess -ExpectedPython $scanExe -ExpectedScript $scanScript -GetProcesses { $backupProcesses }
Check 'run.py.bak is not treated as JiaAgent' ($scan.Ok -and @($scan.Pids).Count -eq 0)

Write-Host 'Stop decision without PID state:'
$outcome = Get-JiaNoPidStopOutcome -HealthChecked $true -HealthOk $true -ScanOk $true -JiaPids @()
Check 'healthy endpoint without trusted PID refuses success' ($outcome.ExitCode -eq 1 -and $outcome.Code -eq 'health-reachable')

$outcome = Get-JiaNoPidStopOutcome -HealthChecked $false -HealthOk $false -ScanOk $true -JiaPids @() -HealthError 'JIA_HOST is not a loopback address'
Check 'unchecked health refuses success' ($outcome.ExitCode -eq 1 -and $outcome.Code -eq 'health-unchecked')

$outcome = Get-JiaNoPidStopOutcome -HealthChecked $true -HealthOk $false -ScanOk $false -JiaPids @() -ScanReason 'process-query-failed'
Check 'process scan failure refuses success' ($outcome.ExitCode -eq 1 -and $outcome.Code -eq 'scan-unconfirmed')

$outcome = Get-JiaNoPidStopOutcome -HealthChecked $true -HealthOk $false -ScanOk $true -JiaPids @(4321)
Check 'untracked matching process refuses success' ($outcome.ExitCode -eq 1 -and $outcome.Code -eq 'jia-process-found')

$outcome = Get-JiaNoPidStopOutcome -HealthChecked $true -HealthOk $false -ScanOk $true -JiaPids @()
Check 'health down and no matching process confirms stopped' ($outcome.ExitCode -eq 0 -and $outcome.Code -eq 'no-jia-found')

Write-Host 'Stop script state-file failures (temp copy, no real Jia):'
$copyRoot = Join-Path $env:TEMP ('jia-stop-copy-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$copyScripts = Join-Path $copyRoot 'scripts\windows'
$copyData = Join-Path $copyRoot 'data'
New-Item -ItemType Directory -Path $copyScripts | Out-Null
New-Item -ItemType Directory -Path $copyData | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'jia_common.ps1') -Destination $copyScripts
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'stop_jia.ps1') -Destination $copyScripts
$copyStop = Join-Path $copyScripts 'stop_jia.ps1'
$copyPid = Join-Path $copyData 'jiaagent.pid'
$powershellExe = Join-Path $PSHOME 'powershell.exe'
$copyClosedPort = Get-FreeLoopbackPort
Set-Content -LiteralPath (Join-Path $copyRoot '.env') -Value ("JIA_HOST=127.0.0.1`nJIA_PORT=" + $copyClosedPort) -Encoding ascii
try {
    Set-Content -LiteralPath $copyPid -Value 'not-json' -Encoding ascii
    $before = Get-Content -LiteralPath $copyPid -Raw
    $null = & $powershellExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $copyStop
    $code = $LASTEXITCODE
    $after = Get-Content -LiteralPath $copyPid -Raw
    Check 'corrupt PID state: stop exits nonzero' ($code -ne 0) ("code=" + $code)
    Check 'corrupt PID state: state file is preserved unchanged' ((Test-Path -LiteralPath $copyPid) -and $after -eq $before)

    Set-Content -LiteralPath $copyPid -Value '' -Encoding ascii
    $null = & $powershellExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $copyStop
    $code = $LASTEXITCODE
    Check 'empty PID state: stop exits nonzero' ($code -ne 0) ("code=" + $code)
    Check 'empty PID state: state file is preserved' (Test-Path -LiteralPath $copyPid)

    Remove-Item -LiteralPath $copyPid -Force
    New-Item -ItemType Directory -Path $copyPid | Out-Null
    $null = & $powershellExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $copyStop
    $code = $LASTEXITCODE
    Check 'unreadable PID state: stop exits nonzero' ($code -ne 0) ("code=" + $code)
    Check 'unreadable PID state: path is preserved' (Test-Path -LiteralPath $copyPid -PathType Container)

    Remove-Item -LiteralPath $copyPid -Recurse -Force
    $null = & $powershellExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $copyStop
    $code = $LASTEXITCODE
    Check 'missing PID + health down + completed scan: stop exits 0' ($code -eq 0) ("code=" + $code)
    Check 'missing PID state is not created by stop' (-not (Test-Path -LiteralPath $copyPid))

    $tempBlocker = Join-Path $copyRoot 'temp-is-a-file'
    Set-Content -LiteralPath $tempBlocker -Value 'not a directory' -Encoding ascii
    $savedTemp = $env:TEMP
    $savedTmp = $env:TMP
    try {
        $env:TEMP = $tempBlocker
        $env:TMP = $tempBlocker
        $null = & $powershellExe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $copyStop
        $code = $LASTEXITCODE
    } finally {
        $env:TEMP = $savedTemp
        $env:TMP = $savedTmp
        Remove-Item -LiteralPath $tempBlocker -Force -ErrorAction SilentlyContinue
    }
    Check 'missing PID + health temp failure: stop exits nonzero' ($code -ne 0) ("code=" + $code)
} finally {
    Remove-Item -LiteralPath $copyRoot -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host 'Local named event:'
$eventName = 'Local\JiaAgent-Shutdown-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
$eventHandle = [System.Threading.EventWaitHandle]::new($false, [System.Threading.EventResetMode]::AutoReset, $eventName)
try {
    $opened = [System.Threading.EventWaitHandle]::OpenExisting($eventName)
    try {
        Check 'OpenExisting used by stop script works' ($null -ne $opened)
        [void]$opened.Set()
        Check 'event is signalled after Set()' ($eventHandle.WaitOne(0))
    } finally {
        $opened.Dispose()
    }
} finally {
    $eventHandle.Dispose()
}
Check 'missing shutdown event fails' (Test-Throws { [System.Threading.EventWaitHandle]::OpenExisting('Local\JiaAgent-Shutdown-000000000000') })

Write-Host 'Launcher cmd invocation (harmless probe, no Jia):'
$probe = Join-Path $env:TEMP ('jia-cmd-probe-' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.py')
Set-Content -LiteralPath $probe -Value 'import sys; print("OUT"); print("ERR", file=sys.stderr); sys.exit(7)' -Encoding ascii
try {
    $projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    Check 'project virtualenv python exists for probe' (Test-Path -LiteralPath $venvPython)
    $cmdLine = '""{0}" "{1}" 2>&1"' -f $venvPython, $probe
    $captured = @()
    & cmd.exe /d /c $cmdLine | ForEach-Object { $captured += [string]$_ }
    $probeCode = $LASTEXITCODE
    Check 'launcher cmd captures stdout' ($captured -contains 'OUT')
    Check 'launcher cmd merges stderr into stdout' ($captured -contains 'ERR')
    Check 'launcher cmd propagates child exit code' ($probeCode -eq 7) ("code=" + $probeCode)
} finally {
    Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
}

Write-Host 'Auto-start ownership:'
$autostartCommonPath = Join-Path $PSScriptRoot 'autostart_common.ps1'
if (Test-Path -LiteralPath $autostartCommonPath) {
    . $autostartCommonPath
}

$ownershipFunctions = @(
    'Get-JiaTaskOwnership',
    'Get-JiaShortcutOwnership',
    'Invoke-JiaTaskInstall',
    'Invoke-JiaTaskUninstall',
    'Invoke-JiaShortcutInstall',
    'Invoke-JiaShortcutUninstall'
)
$missingOwnership = @()
foreach ($requiredFunction in $ownershipFunctions) {
    if ($null -eq (Get-Command $requiredFunction -ErrorAction SilentlyContinue)) {
        $missingOwnership += $requiredFunction
    }
}
Check 'ownership helpers are defined' ($missingOwnership.Count -eq 0) ($missingOwnership -join ', ')

$resolverDefined = $null -ne (Get-Command 'Resolve-JiaWindowsPowerShellPath' -ErrorAction SilentlyContinue)
Check 'Windows PowerShell path resolver is defined' $resolverDefined
if ($resolverDefined) {
    $resolverRoot = Join-Path $env:TEMP ('jia-powershell-resolver-' + [guid]::NewGuid().ToString('N'))
    try {
        $fakeWindows = Join-Path $resolverRoot 'Windows'
        $fakeDir = Join-Path $fakeWindows 'System32\WindowsPowerShell\v1.0'
        New-Item -ItemType Directory -Path $fakeDir | Out-Null
        $fakePowerShell = Join-Path $fakeDir 'powershell.exe'
        Set-Content -LiteralPath $fakePowerShell -Value '' -Encoding ascii
        $resolved = Resolve-JiaWindowsPowerShellPath -WindowsDirectory $fakeWindows
        Check 'existing Windows PowerShell path is accepted' ($resolved.Ok -and (Test-JiaSamePath $resolved.Path $fakePowerShell))

        $missingWindows = Join-Path $resolverRoot 'MissingWindows'
        $resolved = Resolve-JiaWindowsPowerShellPath -WindowsDirectory $missingWindows
        Check 'missing Windows PowerShell path is refused' ((-not $resolved.Ok) -and -not [string]::IsNullOrWhiteSpace([string]$resolved.Reason))

        $notAWindowsDir = Join-Path $resolverRoot 'windows-is-a-file'
        Set-Content -LiteralPath $notAWindowsDir -Value 'not a directory' -Encoding ascii
        $resolved = Resolve-JiaWindowsPowerShellPath -WindowsDirectory $notAWindowsDir
        Check 'Windows directory that is a file is refused' (-not $resolved.Ok)

        $resolved = Resolve-JiaWindowsPowerShellPath
        Check 'system Windows PowerShell 5.1 executable exists' $resolved.Ok
    } finally {
        Remove-Item -LiteralPath $resolverRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function New-TestTaskInfo {
    param(
        [bool]$Exists = $true,
        [bool]$Readable = $true,
        [string]$Execute = '',
        [string]$Arguments = '',
        [string]$WorkingDirectory = '',
        [int]$ActionCount = 1,
        [int]$ActionType = 0
    )
    return [pscustomobject]@{
        Exists = $Exists
        Readable = $Readable
        Execute = $Execute
        Arguments = $Arguments
        WorkingDirectory = $WorkingDirectory
        ActionCount = $ActionCount
        ActionType = $ActionType
        Error = ''
    }
}

function New-TestShortcutInfo {
    param(
        [bool]$Exists = $true,
        [bool]$Readable = $true,
        [string]$TargetPath = '',
        [string]$Arguments = '',
        [string]$WorkingDirectory = ''
    )
    return [pscustomobject]@{
        Exists = $Exists
        Readable = $Readable
        TargetPath = $TargetPath
        Arguments = $Arguments
        WorkingDirectory = $WorkingDirectory
        Error = ''
    }
}

if ($missingOwnership.Count -eq 0) {
    $ownRoot = 'D:\JiaAgent'
    $ownExe = Join-Path $PSHOME 'powershell.exe'
    $ownLauncher = 'D:\JiaAgent\scripts\windows\start_jia.ps1'
    $ownBaseArgs = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $ownLauncher + '"'
    $ownAllowed = @($ownBaseArgs, ($ownBaseArgs + ' -EnableWeChat'))
    $foreignLauncher = 'C:\OtherApp\start.ps1'
    $foreignArgs = '-File "' + $foreignLauncher + '"'

    $absentTask = New-TestTaskInfo -Exists $false
    $ownedTask = New-TestTaskInfo -Execute $ownExe -Arguments $ownBaseArgs -WorkingDirectory $ownRoot
    $enabledTask = New-TestTaskInfo -Execute $ownExe -Arguments ($ownBaseArgs + ' -EnableWeChat') -WorkingDirectory $ownRoot
    $foreignExeTask = New-TestTaskInfo -Execute 'C:\OtherApp\powershell.exe' -Arguments $ownBaseArgs -WorkingDirectory $ownRoot
    $foreignArgsTask = New-TestTaskInfo -Execute $ownExe -Arguments $foreignArgs -WorkingDirectory $ownRoot
    $foreignDirTask = New-TestTaskInfo -Execute $ownExe -Arguments $ownBaseArgs -WorkingDirectory 'C:\OtherApp'
    $relativeTask = New-TestTaskInfo -Execute '.\powershell.exe' -Arguments $ownBaseArgs -WorkingDirectory $ownRoot
    $unreadableTask = New-TestTaskInfo -Exists $true -Readable $false -Execute $ownExe -Arguments $ownBaseArgs -WorkingDirectory $ownRoot
    $multiActionTask = New-TestTaskInfo -Execute $ownExe -Arguments $ownBaseArgs -WorkingDirectory $ownRoot -ActionCount 2
    $nonExecTask = New-TestTaskInfo -Execute $ownExe -Arguments $ownBaseArgs -WorkingDirectory $ownRoot -ActionType 1

    $decision = Get-JiaTaskOwnership -Info $absentTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'absent task is safe to create' ($decision.Allowed -and $decision.State -eq 'Absent')
    $decision = Get-JiaTaskOwnership -Info $ownedTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'owned task allows update' ($decision.Allowed -and $decision.State -eq 'Owned')
    $decision = Get-JiaTaskOwnership -Info $enabledTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'owned task with -EnableWeChat allows update' ($decision.Allowed -and $decision.State -eq 'Owned')
    $decision = Get-JiaTaskOwnership -Info $foreignExeTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'foreign task executable refuses' (-not $decision.Allowed -and $decision.State -eq 'Foreign')
    $decision = Get-JiaTaskOwnership -Info $foreignArgsTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'foreign task arguments refuse' (-not $decision.Allowed -and $decision.State -eq 'Foreign')
    $decision = Get-JiaTaskOwnership -Info $foreignDirTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'foreign task working directory refuses' (-not $decision.Allowed -and $decision.State -eq 'Foreign')
    $decision = Get-JiaTaskOwnership -Info $relativeTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'relative task executable refuses' (-not $decision.Allowed -and $decision.State -eq 'Foreign')
    $decision = Get-JiaTaskOwnership -Info $unreadableTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'unreadable task refuses' (-not $decision.Allowed -and $decision.State -eq 'Unreadable')
    $decision = Get-JiaTaskOwnership -Info $multiActionTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'task with multiple actions refuses' (-not $decision.Allowed)
    $decision = Get-JiaTaskOwnership -Info $nonExecTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'task with a non-exec action refuses' (-not $decision.Allowed)

    $absentShortcut = New-TestShortcutInfo -Exists $false
    $ownedShortcut = New-TestShortcutInfo -TargetPath $ownExe -Arguments $ownBaseArgs -WorkingDirectory $ownRoot
    $enabledShortcut = New-TestShortcutInfo -TargetPath $ownExe -Arguments ($ownBaseArgs + ' -EnableWeChat') -WorkingDirectory $ownRoot
    $foreignTargetShortcut = New-TestShortcutInfo -TargetPath 'C:\OtherApp\other.exe' -Arguments $ownBaseArgs -WorkingDirectory $ownRoot
    $foreignArgsShortcut = New-TestShortcutInfo -TargetPath $ownExe -Arguments $foreignArgs -WorkingDirectory $ownRoot
    $foreignDirShortcut = New-TestShortcutInfo -TargetPath $ownExe -Arguments $ownBaseArgs -WorkingDirectory 'C:\OtherApp'
    $relativeTargetShortcut = New-TestShortcutInfo -TargetPath '.\powershell.exe' -Arguments $ownBaseArgs -WorkingDirectory $ownRoot
    $unreadableShortcut = New-TestShortcutInfo -Exists $true -Readable $false

    $decision = Get-JiaShortcutOwnership -Info $absentShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'absent shortcut is safe to create' ($decision.Allowed -and $decision.State -eq 'Absent')
    $decision = Get-JiaShortcutOwnership -Info $ownedShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'owned shortcut allows update' ($decision.Allowed -and $decision.State -eq 'Owned')
    $decision = Get-JiaShortcutOwnership -Info $enabledShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'owned shortcut with -EnableWeChat allows update' ($decision.Allowed -and $decision.State -eq 'Owned')
    $decision = Get-JiaShortcutOwnership -Info $foreignTargetShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'foreign shortcut target refuses' (-not $decision.Allowed -and $decision.State -eq 'Foreign')
    $decision = Get-JiaShortcutOwnership -Info $foreignArgsShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'foreign shortcut arguments refuse' (-not $decision.Allowed -and $decision.State -eq 'Foreign')
    $decision = Get-JiaShortcutOwnership -Info $foreignDirShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'foreign shortcut working directory refuses' (-not $decision.Allowed -and $decision.State -eq 'Foreign')
    $decision = Get-JiaShortcutOwnership -Info $relativeTargetShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'relative shortcut target refuses' (-not $decision.Allowed -and $decision.State -eq 'Foreign')
    $decision = Get-JiaShortcutOwnership -Info $unreadableShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
    Check 'unreadable shortcut refuses' (-not $decision.Allowed -and $decision.State -eq 'Unreadable')

    $emptyTask = Read-JiaScheduledTaskInfo -TaskName ''
    Check 'empty task name fails closed' ((-not $emptyTask.Readable) -and (-not $emptyTask.Exists))
    $emptyShortcut = Read-JiaShortcutInfo -Path ''
    Check 'empty shortcut path fails closed' ((-not $emptyShortcut.Readable) -and (-not $emptyShortcut.Exists))
    $missingShortcutPath = Join-Path ([IO.Path]::GetTempPath()) ('JiaAgent-Missing-' + [guid]::NewGuid().ToString('N') + '.lnk')
    $missingShortcut = Read-JiaShortcutInfo -Path $missingShortcutPath
    Check 'missing shortcut is reported as absent' ($missingShortcut.Readable -and (-not $missingShortcut.Exists))
    $badShortcutPath = Join-Path ([IO.Path]::GetTempPath()) ('JiaAgent-Bad-' + [guid]::NewGuid().ToString('N') + '.lnk')
    try {
        Set-Content -LiteralPath $badShortcutPath -Value 'not a shortcut' -Encoding ascii
        $badShortcut = Read-JiaShortcutInfo -Path $badShortcutPath
        Check 'unreadable shortcut properties fail closed' ($badShortcut.Exists -and (-not $badShortcut.Readable))
    } finally {
        Remove-Item -LiteralPath $badShortcutPath -Force -ErrorAction SilentlyContinue
    }
    $roundTripPath = Join-Path ([IO.Path]::GetTempPath()) ('JiaAgent-RoundTrip-' + [guid]::NewGuid().ToString('N') + '.lnk')
    try {
        $shell = New-Object -ComObject WScript.Shell
        $roundTripShortcut = $shell.CreateShortcut($roundTripPath)
        $roundTripShortcut.TargetPath = $ownExe
        $roundTripShortcut.Arguments = $ownBaseArgs
        $roundTripShortcut.WorkingDirectory = $ownRoot
        $roundTripShortcut.Save()
        $roundTrip = Read-JiaShortcutInfo -Path $roundTripPath
        Check 'shortcut target/arguments/workdir round-trip' ($roundTrip.Exists -and $roundTrip.Readable -and (Test-JiaSamePath $roundTrip.TargetPath $ownExe) -and $roundTrip.Arguments -eq $ownBaseArgs -and (Test-JiaSamePath $roundTrip.WorkingDirectory $ownRoot))
        $roundTripDecision = Get-JiaShortcutOwnership -Info $roundTrip -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed
        Check 'round-tripped project shortcut is recognized as owned' ($roundTripDecision.Allowed -and $roundTripDecision.State -eq 'Owned')
    } finally {
        Remove-Item -LiteralPath $roundTripPath -Force -ErrorAction SilentlyContinue
    }

    $script:ownershipCalls = @{ Count = 0 }
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaTaskInstall -Info $unreadableTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -CreateOrUpdate { $script:ownershipCalls.Count++ }
    Check 'unreadable task: install refuses without registering' ((-not $result.Allowed) -and $result.State -eq 'Unreadable' -and $script:ownershipCalls.Count -eq 0)
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaTaskUninstall -Info $unreadableTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -Remove { $script:ownershipCalls.Count++ }
    Check 'unreadable task: uninstall refuses without deleting' ((-not $result.Allowed) -and $result.State -eq 'Unreadable' -and $script:ownershipCalls.Count -eq 0)
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaShortcutInstall -Info $unreadableShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -CreateOrUpdate { $script:ownershipCalls.Count++ }
    Check 'unreadable shortcut: install refuses without saving' ((-not $result.Allowed) -and $result.State -eq 'Unreadable' -and $script:ownershipCalls.Count -eq 0)
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaShortcutUninstall -Info $unreadableShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -Remove { $script:ownershipCalls.Count++ }
    Check 'unreadable shortcut: uninstall refuses without deleting' ((-not $result.Allowed) -and $result.State -eq 'Unreadable' -and $script:ownershipCalls.Count -eq 0)

    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaTaskInstall -Info $foreignExeTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -CreateOrUpdate { $script:ownershipCalls.Count++ }
    Check 'foreign task: install refuses without registering' ((-not $result.Allowed) -and $script:ownershipCalls.Count -eq 0)
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaTaskInstall -Info $absentTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -CreateOrUpdate { $script:ownershipCalls.Count++ }
    Check 'absent task: install registers once' ($result.Allowed -and $result.Changed -and $script:ownershipCalls.Count -eq 1)
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaTaskInstall -Info $ownedTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -CreateOrUpdate { $script:ownershipCalls.Count++ }
    Check 'owned task: install updates once' ($result.Allowed -and $result.Changed -and $script:ownershipCalls.Count -eq 1)
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaTaskUninstall -Info $foreignExeTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -Remove { $script:ownershipCalls.Count++ }
    Check 'foreign task: uninstall refuses without deleting' ((-not $result.Allowed) -and $script:ownershipCalls.Count -eq 0)
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaTaskUninstall -Info $absentTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -Remove { $script:ownershipCalls.Count++ }
    Check 'absent task: uninstall is a no-op' ($result.Allowed -and -not $result.Changed -and $script:ownershipCalls.Count -eq 0)
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaTaskUninstall -Info $ownedTask -ExpectedExecute $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -Remove { $script:ownershipCalls.Count++ }
    Check 'owned task: uninstall removes once' ($result.Allowed -and $result.Changed -and $script:ownershipCalls.Count -eq 1)

    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaShortcutInstall -Info $foreignTargetShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -CreateOrUpdate { $script:ownershipCalls.Count++ }
    Check 'foreign shortcut: install refuses without saving' ((-not $result.Allowed) -and $script:ownershipCalls.Count -eq 0)
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaShortcutInstall -Info $absentShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -CreateOrUpdate { $script:ownershipCalls.Count++ }
    Check 'absent shortcut: install saves once' ($result.Allowed -and $result.Changed -and $script:ownershipCalls.Count -eq 1)
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaShortcutInstall -Info $ownedShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -CreateOrUpdate { $script:ownershipCalls.Count++ }
    Check 'owned shortcut: install saves once' ($result.Allowed -and $result.Changed -and $script:ownershipCalls.Count -eq 1)
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaShortcutUninstall -Info $foreignTargetShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -Remove { $script:ownershipCalls.Count++ }
    Check 'foreign shortcut: uninstall refuses without deleting' ((-not $result.Allowed) -and $script:ownershipCalls.Count -eq 0)
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaShortcutUninstall -Info $absentShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -Remove { $script:ownershipCalls.Count++ }
    Check 'absent shortcut: uninstall is a no-op' ($result.Allowed -and -not $result.Changed -and $script:ownershipCalls.Count -eq 0)
    $script:ownershipCalls.Count = 0
    $result = Invoke-JiaShortcutUninstall -Info $ownedShortcut -ExpectedTarget $ownExe -ExpectedRoot $ownRoot -AllowedArguments $ownAllowed -Remove { $script:ownershipCalls.Count++ }
    Check 'owned shortcut: uninstall removes once' ($result.Allowed -and $result.Changed -and $script:ownershipCalls.Count -eq 1)
}

Write-Host 'Static script constraints:'
$folder = $PSScriptRoot
$stopText = Get-Content -LiteralPath (Join-Path $folder 'stop_jia.ps1') -Raw
$startText = Get-Content -LiteralPath (Join-Path $folder 'start_jia.ps1') -Raw
$statusText = Get-Content -LiteralPath (Join-Path $folder 'status_jia.ps1') -Raw
$commonText = Get-Content -LiteralPath (Join-Path $folder 'jia_common.ps1') -Raw
$autostartCommonText = Get-Content -LiteralPath (Join-Path $folder 'autostart_common.ps1') -Raw
$installText = Get-Content -LiteralPath (Join-Path $folder 'install_autostart.ps1') -Raw
$uninstallText = Get-Content -LiteralPath (Join-Path $folder 'uninstall_autostart.ps1') -Raw
Check 'stop script never calls taskkill' ($stopText -notmatch '(?i)taskkill')
Check 'stop script never walks or kills a process tree' ($stopText -notmatch '(?i)ParentProcessId' -and $stopText -notmatch '(?i)Get-Process[^\r\n]*\|')
Check 'stop script uses the local named event for graceful shutdown' ($stopText -match 'EventWaitHandle\]::OpenExisting')
Check 'stop script force-stops only the verified PID' ($stopText -match 'Stop-Process -Id \$state\.Pid -Force')
Check 'stop script fails closed on unverifiable identity' ($stopText -match "'Unverified'" -and $stopText -match 'Refusing to force-stop')
Check 'start script goes through the redacting wrapper' ($startText -match 'run_jia_redacted\.py')
Check 'status script reuses strict health validation' ($statusText -match 'Get-JiaHealth' -and $commonText -match 'Test-JiaHealthPayload')
Check 'health validation binds service name and status' ($commonText -match 'JiaAgent' -and $commonText -match "'ok'")
Check 'install script verifies task ownership before registering' ($installText -match 'Read-JiaScheduledTaskInfo' -and $installText -match 'Get-JiaTaskOwnership' -and $installText -match 'Invoke-JiaTaskInstall')
Check 'install script verifies shortcut ownership before saving' ($installText -match 'Read-JiaShortcutInfo' -and $installText -match 'Get-JiaShortcutOwnership' -and $installText -match 'Invoke-JiaShortcutInstall')
Check 'uninstall script verifies task ownership before removal' ($uninstallText -match 'Read-JiaScheduledTaskInfo' -and $uninstallText -match 'Invoke-JiaTaskUninstall')
Check 'uninstall script verifies shortcut ownership before removal' ($uninstallText -match 'Read-JiaShortcutInfo' -and $uninstallText -match 'Invoke-JiaShortcutUninstall')
Check 'uninstall task helper is gated by ownership check' ($autostartCommonText -match '(?s)function Invoke-JiaTaskUninstall.*?Get-JiaTaskOwnership')
Check 'uninstall shortcut helper is gated by ownership check' ($autostartCommonText -match '(?s)function Invoke-JiaShortcutUninstall.*?Get-JiaShortcutOwnership')
Check 'uninstall script no longer deletes tasks or links by name alone' ($uninstallText -notmatch 'Get-ScheduledTask\s' -and $uninstallText -notmatch 'Test-Path -LiteralPath \$shortcutPath')
Check 'stop script classifies PID state instead of treating every failure as absent' ($stopText -match 'Read-JiaPidStateResult' -and $stopText -match "'Corrupt'" -and $stopText -match "'Unreadable'" -and $stopText -match "'Missing'")
Check 'stop script checks health and process identity when PID state is missing' ($stopText -match 'Get-JiaHealth' -and $stopText -match 'Find-JiaProcess' -and $stopText -match 'Get-JiaNoPidStopOutcome')
Check 'stop script treats any unreliable health probe as unchecked' ($stopText -match '\$health\.Checked' -and $stopText -notmatch 'CommandNotFoundException')
Check 'status script distinguishes a failed health probe from a negative response' ($statusText -match '\$health\.Checked' -and $statusText -match 'not checked')
Check 'status script distinguishes missing, corrupt, and unreadable PID state' ($statusText -match 'Read-JiaPidStateResult' -and $statusText -match 'Corrupt' -and $statusText -match 'Unreadable')
Check 'install script uses a validated fixed Windows PowerShell path' ($installText -notmatch '\$PSHOME' -and $installText -match 'Resolve-JiaWindowsPowerShellPath' -and $installText -match '\$powershellResolution\.Ok')
Check 'install checks PowerShell availability before building the task action' ($installText.IndexOf('Resolve-JiaWindowsPowerShellPath') -ge 0 -and $installText.IndexOf('Resolve-JiaWindowsPowerShellPath') -lt $installText.IndexOf('New-ScheduledTaskAction'))
Check 'uninstall script uses a validated fixed Windows PowerShell path' ($uninstallText -notmatch '\$PSHOME' -and $uninstallText -match 'Resolve-JiaWindowsPowerShellPath' -and $uninstallText -match '\$powershellResolution\.Ok')
Check 'uninstall checks PowerShell availability before reading ownership' ($uninstallText.IndexOf('Resolve-JiaWindowsPowerShellPath') -ge 0 -and $uninstallText.IndexOf('Resolve-JiaWindowsPowerShellPath') -lt $uninstallText.IndexOf('Read-JiaScheduledTaskInfo'))

$total = $script:passed + $script:failed
Write-Host ''
if ($script:failed -eq 0) {
    Write-Host ("Result: " + $script:passed + "/" + $total + " passed [OK]")
    exit 0
}
Write-Host ("Result: " + $script:passed + "/" + $total + " passed, " + $script:failed + " failed [!!]")
exit 1
