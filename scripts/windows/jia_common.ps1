# Shared helpers for JiaAgent Windows scripts.
#
# Dot-source this file from status_jia.ps1 / stop_jia.ps1. It is also
# dot-sourced by test_windows_scripts.ps1, so it must not start services,
# read credentials, or perform any process termination.
#
# This file is intentionally ASCII-only: Windows PowerShell 5.1 may read a
# UTF-8 file without BOM using the ANSI code page, which corrupts Chinese
# identifiers and messages. Chinese user-facing text stays in the docs.

function Test-JiaLoopbackHost {
    param([string]$HostValue)
    if ([string]::IsNullOrWhiteSpace($HostValue)) { return $false }
    $name = $HostValue.Trim()
    if ($name.StartsWith('[') -and $name.EndsWith(']')) {
        $name = $name.Substring(1, $name.Length - 2)
    }
    $name = $name.TrimEnd('.')
    if ($name -ieq 'localhost') { return $true }
    $address = $null
    if ([System.Net.IPAddress]::TryParse($name, [ref]$address)) {
        # IsLoopback is a STATIC method in .NET Framework; the instance
        # property form silently yields $null and would reject 127.0.0.1.
        return [System.Net.IPAddress]::IsLoopback($address)
    }
    return $false
}

function Format-JiaHost {
    param([string]$HostValue)
    $name = $HostValue.Trim()
    if ($name.StartsWith('[') -and $name.EndsWith(']')) {
        $name = $name.Substring(1, $name.Length - 2)
    }
    $name = $name.TrimEnd('.')
    if ($name.Contains(':')) { return "[$name]" }
    return $name
}

function Format-JiaBaseUrl {
    param([string]$HostValue, [int]$Port)
    if (-not (Test-JiaLoopbackHost $HostValue)) {
        throw "JIA_HOST is not a loopback address"
    }
    if ($Port -lt 1 -or $Port -gt 65535) {
        throw "JIA_PORT is outside 1..65535"
    }
    return "http://$(Format-JiaHost $HostValue):$Port"
}

function Get-JiaEnvValue {
    param([string]$Root, [string]$Name)
    $fromProcess = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if (-not [string]::IsNullOrWhiteSpace($fromProcess)) { return $fromProcess.Trim() }

    $envFile = Join-Path $Root '.env'
    if (-not (Test-Path -LiteralPath $envFile)) { return $null }
    $pattern = '^\s*' + [regex]::Escape($Name) + '\s*=\s*(.*)$'
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8 -ErrorAction SilentlyContinue) {
        if ($line -match $pattern) {
            $value = $Matches[1].Trim()
            # Strip a trailing inline comment only when it follows whitespace.
            if ($value -match '^(.*?)(?:\s+#.*)?$') { $value = $Matches[1].Trim() }
            if ($value.Length -ge 2) {
                $doubleQuoted = $value.StartsWith('"') -and $value.EndsWith('"')
                $singleQuoted = $value.StartsWith("'") -and $value.EndsWith("'")
                if ($doubleQuoted -or $singleQuoted) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
            }
            return $value
        }
    }
    return $null
}

function Resolve-JiaEndpoint {
    param([string]$Root, $PidState = $null)
    $hostValue = $null
    $portText = $null
    $source = 'env'

    # A live instance's own state is more accurate than the current .env
    # (the file may have been edited after startup). Only use it when it
    # carries both host and port.
    if ($null -ne $PidState -and -not $PidState.Legacy) {
        $stateHost = [string]$PidState.Host
        if (-not [string]::IsNullOrWhiteSpace($stateHost) -and $null -ne $PidState.Port) {
            $hostValue = $stateHost
            $portText = [string]$PidState.Port
            $source = 'pid'
        }
    }

    if ([string]::IsNullOrWhiteSpace($hostValue)) {
        $hostValue = Get-JiaEnvValue -Root $Root -Name 'JIA_HOST'
        if ([string]::IsNullOrWhiteSpace($hostValue)) { $hostValue = '127.0.0.1' }
        $portText = Get-JiaEnvValue -Root $Root -Name 'JIA_PORT'
        if ([string]::IsNullOrWhiteSpace($portText)) { $portText = '8000' }
        $source = 'env'
    }

    if (-not (Test-JiaLoopbackHost $hostValue)) {
        return [pscustomobject]@{
            Ok = $false
            Reason = 'JIA_HOST is not a loopback address; refusing to check an arbitrary host'
            Host = $hostValue
            Port = 0
            BaseUrl = $null
            Source = $source
        }
    }

    $port = 0
    if (-not [int]::TryParse($portText.Trim(), [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
        return [pscustomobject]@{
            Ok = $false
            Reason = 'JIA_PORT is not an integer in 1..65535'
            Host = $hostValue
            Port = 0
            BaseUrl = $null
            Source = $source
        }
    }

    return [pscustomobject]@{
        Ok = $true
        Reason = ''
        Host = $hostValue
        Port = $port
        BaseUrl = (Format-JiaBaseUrl $hostValue $port)
        Source = $source
    }
}

function Test-JiaHealthPayload {
    param([int]$StatusCode, [string]$Body, [string]$ContentType = '')
    if ($StatusCode -ne 200) { return $false }
    if (-not [string]::IsNullOrWhiteSpace($ContentType) -and $ContentType -notmatch '(?i)application/json') {
        return $false
    }
    try {
        $data = $Body | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return $false
    }
    if ($null -eq $data) { return $false }
    if ([string]$data.status -ne 'ok') { return $false }
    if ([string]$data.name -ne 'JiaAgent') { return $false }
    return ([string]$data.version -match '^\d+\.\d+\.\d+$')
}

function Get-JiaHealth {
    param([string]$BaseUrl, [scriptblock]$Fetch = $null)
    $url = $BaseUrl.TrimEnd('/') + '/api/health'

    if ($null -eq $Fetch) {
        $Fetch = {
            param([string]$Target)
            $temp = [IO.Path]::GetTempFileName()
            try {
                # Windows PowerShell 5.1 turns a native command's stderr into
                # ErrorRecords. Under the callers' ErrorActionPreference=Stop,
                # curl's normal "connection refused" message would otherwise
                # abort the probe and make an ordinary unreachable endpoint
                # look like an internal error. Continue only around the native
                # call; explicit -ErrorAction Stop still surfaces read errors.
                $savedErrorAction = $ErrorActionPreference
                try {
                    $ErrorActionPreference = 'Continue'
                    # One line makes the status/content-type boundary unambiguous.
                    $raw = & curl.exe -sS --noproxy '*' --max-time 3 `
                        -o $temp -w "%{http_code}|%{content_type}" $Target 2>$null
                } finally {
                    $ErrorActionPreference = $savedErrorAction
                }
                $parts = ([string]$raw).Split('|', 2)
                if ($parts.Count -ne 2) {
                    return [pscustomobject]@{
                        StatusCode = 0
                        Body = ''
                        ContentType = ''
                        Error = 'curl returned no status'
                    }
                }
                $code = 0
                if (-not [int]::TryParse($parts[0].Trim(), [ref]$code)) { $code = 0 }
                $body = ''
                if (Test-Path -LiteralPath $temp) {
                    # A read failure is an internal probe failure, not a
                    # negative health response. Let the catch report it.
                    $body = [string](Get-Content -LiteralPath $temp -Raw -Encoding UTF8 -ErrorAction Stop)
                }
                return [pscustomobject]@{
                    StatusCode = $code
                    Body = $body
                    ContentType = $parts[1].Trim()
                    Error = ''
                }
            } catch {
                return [pscustomobject]@{
                    StatusCode = 0
                    Body = ''
                    ContentType = ''
                    Error = $_.Exception.GetType().Name
                }
            } finally {
                Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
            }
        }
    }

    $response = $null
    try {
        $response = & $Fetch $url
    } catch {
        $response = [pscustomobject]@{
            StatusCode = 0
            Body = ''
            ContentType = ''
            Error = $_.Exception.GetType().Name
        }
    }
    if ($null -eq $response) {
        return [pscustomobject]@{
            Ok = $false
            Checked = $false
            StatusCode = 0
            BaseUrl = $BaseUrl
            Error = 'empty response'
            Data = $null
            ContentType = ''
        }
    }

    $statusCode = 0
    if (-not [int]::TryParse([string]$response.StatusCode, [ref]$statusCode)) { $statusCode = 0 }
    $body = [string]$response.Body
    $contentType = [string]$response.ContentType
    $ok = Test-JiaHealthPayload -StatusCode $statusCode -Body $body -ContentType $contentType
    $err = [string]$response.Error
    $parsed = $null
    try { $parsed = $body | ConvertFrom-Json -ErrorAction Stop } catch { $parsed = $null }
    # Only an empty Error means the probe actually completed. A normal
    # connection refusal is still a completed probe (curl returns 000).
    $checked = [string]::IsNullOrWhiteSpace($err)

    return [pscustomobject]@{
        Ok = ($ok -and $checked)
        Checked = $checked
        StatusCode = $statusCode
        BaseUrl = $BaseUrl
        Error = $err
        Data = $parsed
        ContentType = $contentType
    }
}

function New-JiaPidStateResult {
    param([string]$State, $Value, [string]$Reason, [bool]$Exists)
    return [pscustomobject]@{
        State = $State      # Missing / Valid / Corrupt / Unreadable
        Value = $Value
        Reason = $Reason
        Exists = $Exists
    }
}

# Classify the state file before trusting it. The stop script must distinguish
# "no file" (nothing was recorded) from "the file is damaged or unreadable"
# (something was recorded, but it cannot be trusted). Collapsing all of them
# into $null is what previously allowed stop_jia.ps1 to report success while
# Jia was still running.
function Read-JiaPidStateResult {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return New-JiaPidStateResult -State 'Unreadable' -Value $null -Reason 'state-path-empty' -Exists $false
    }

    try {
        $null = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        return New-JiaPidStateResult -State 'Missing' -Value $null -Reason 'not-found' -Exists $false
    } catch [System.IO.FileNotFoundException] {
        return New-JiaPidStateResult -State 'Missing' -Value $null -Reason 'not-found' -Exists $false
    } catch [System.IO.DirectoryNotFoundException] {
        return New-JiaPidStateResult -State 'Missing' -Value $null -Reason 'not-found' -Exists $false
    } catch {
        return New-JiaPidStateResult -State 'Unreadable' -Value $null -Reason 'state-query-failed' -Exists $false
    }

    try {
        $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
    } catch {
        return New-JiaPidStateResult -State 'Unreadable' -Value $null -Reason 'state-read-failed' -Exists $true
    }

    if ($null -eq $raw) {
        return New-JiaPidStateResult -State 'Corrupt' -Value $null -Reason 'empty' -Exists $true
    }
    $text = ([string]$raw).Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        return New-JiaPidStateResult -State 'Corrupt' -Value $null -Reason 'empty' -Exists $true
    }

    if ($text -match '^\d+$') {
        $legacyPid = 0
        if (-not [int]::TryParse($text, [ref]$legacyPid) -or $legacyPid -le 0) {
            return New-JiaPidStateResult -State 'Corrupt' -Value $null -Reason 'invalid-pid' -Exists $true
        }
        $legacy = [pscustomobject]@{
            Pid = $legacyPid
            Legacy = $true
            StartedAt = $null
            Host = $null
            Port = $null
            ShutdownEvent = $null
        }
        return New-JiaPidStateResult -State 'Valid' -Value $legacy -Reason 'legacy' -Exists $true
    }

    try {
        $data = $text | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return New-JiaPidStateResult -State 'Corrupt' -Value $null -Reason 'invalid-json' -Exists $true
    }
    if ($null -eq $data) {
        return New-JiaPidStateResult -State 'Corrupt' -Value $null -Reason 'invalid-json' -Exists $true
    }

    $statePid = 0
    if (-not [int]::TryParse([string]$data.pid, [ref]$statePid) -or $statePid -le 0) {
        return New-JiaPidStateResult -State 'Corrupt' -Value $null -Reason 'invalid-pid' -Exists $true
    }

    $startedAt = $null
    if ($null -ne $data.started_at) {
        $parsedStarted = 0.0
        if ($data.started_at -is [bool]) {
            return New-JiaPidStateResult -State 'Corrupt' -Value $null -Reason 'invalid-started-at' -Exists $true
        }
        try { $parsedStarted = [double]$data.started_at } catch { $parsedStarted = 0.0 }
        if ($parsedStarted -le 0) {
            return New-JiaPidStateResult -State 'Corrupt' -Value $null -Reason 'invalid-started-at' -Exists $true
        }
        $startedAt = $parsedStarted
    }

    $port = $null
    if ($null -ne $data.port) {
        $parsedPort = 0
        if (-not [int]::TryParse([string]$data.port, [ref]$parsedPort) -or $parsedPort -lt 1 -or $parsedPort -gt 65535) {
            return New-JiaPidStateResult -State 'Corrupt' -Value $null -Reason 'invalid-port' -Exists $true
        }
        $port = $parsedPort
    }

    $eventName = $null
    if ($null -ne $data.shutdown_event) {
        $eventName = [string]$data.shutdown_event
    }

    $state = [pscustomobject]@{
        Pid = $statePid
        Legacy = $false
        StartedAt = $startedAt
        Host = [string]$data.host
        Port = $port
        ShutdownEvent = $eventName
    }
    return New-JiaPidStateResult -State 'Valid' -Value $state -Reason '' -Exists $true
}

# Backward-compatible accessor: callers that only need a usable state get the
# object (or $null). New callers that must distinguish failure modes should
# use Read-JiaPidStateResult instead.
function Read-JiaPidState {
    param([string]$Path)
    $result = Read-JiaPidStateResult -Path $Path
    if ($result.State -eq 'Valid') { return $result.Value }
    return $null
}

function Get-JiaProcessInfo {
    param([int]$PidValue)
    if ($PidValue -le 0) { return $null }
    try {
        $process = Get-Process -Id $PidValue -ErrorAction Stop
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$PidValue" -ErrorAction Stop
        return [pscustomobject]@{
            Pid = $PidValue
            ExecutablePath = [string]$cim.ExecutablePath
            CommandLine = [string]$cim.CommandLine
            StartTimeUtc = $process.StartTime.ToUniversalTime()
        }
    } catch {
        return $null
    }
}

function Test-JiaPidExists {
    param([int]$PidValue)
    if ($PidValue -le 0) { return $false }
    try {
        $process = [System.Diagnostics.Process]::GetProcessById($PidValue)
        if ($null -eq $process) { return $false }
        $process.Dispose()
        return $true
    } catch [System.ArgumentException] {
        # The PID is not running.
        return $false
    } catch {
        # The PID exists but cannot be inspected (for example access denied).
        # Fail closed: callers must treat this as "possibly still running".
        return $true
    }
}

function Test-JiaScriptArgument {
    param([string]$CommandLine, [string]$ScriptPath)
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $false }
    if ([string]::IsNullOrWhiteSpace($ScriptPath)) { return $false }

    $variants = @($ScriptPath)
    if ($ScriptPath.Contains('\')) { $variants += $ScriptPath.Replace('\', '/') }

    foreach ($variant in $variants) {
        $escaped = [regex]::Escape($variant)
        $quoted = '(?i)(?:^|\s)"' + $escaped + '"(?=\s|$)'
        $plain = '(?i)(?:^|\s)' + $escaped + '(?=\s|$)'
        if ([regex]::IsMatch($CommandLine, $quoted)) { return $true }
        if ([regex]::IsMatch($CommandLine, $plain)) { return $true }
    }
    return $false
}

function Test-JiaProcessIdentity {
    param(
        $Info,
        [string]$ExpectedPython,
        [string]$ExpectedScript,
        $ExpectedStartTime = $null
    )
    if ($null -eq $Info) { return $false }
    if ([string]::IsNullOrWhiteSpace([string]$Info.ExecutablePath)) { return $false }
    if ([string]::IsNullOrWhiteSpace([string]$Info.CommandLine)) { return $false }
    if ([string]::IsNullOrWhiteSpace($ExpectedPython)) { return $false }
    if ([string]::IsNullOrWhiteSpace($ExpectedScript)) { return $false }

    try {
        $actualExe = [IO.Path]::GetFullPath([string]$Info.ExecutablePath)
        $expectedExe = [IO.Path]::GetFullPath($ExpectedPython)
    } catch {
        return $false
    }
    if (-not [string]::Equals($actualExe, $expectedExe, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    if (-not (Test-JiaScriptArgument -CommandLine ([string]$Info.CommandLine) -ScriptPath $ExpectedScript)) {
        return $false
    }

    if ($null -ne $ExpectedStartTime -and -not [string]::IsNullOrWhiteSpace([string]$ExpectedStartTime)) {
        $expected = 0.0
        try { $expected = [double]$ExpectedStartTime } catch { $expected = 0.0 }
        if ($expected -le 0) {
            return $false
        }
        if ($null -eq $Info.StartTimeUtc) { return $false }
        try {
            $actual = ([DateTimeOffset]$Info.StartTimeUtc).ToUnixTimeSeconds()
        } catch {
            return $false
        }
        if ([math]::Abs([double]$actual - $expected) -gt 5.0) { return $false }
    }
    return $true
}

# Scan for a JiaAgent process when there is no trusted PID state. This is used
# only to decide whether it is safe to report success. It never stops anything.
#
# Strictness rules:
#   - a python.exe whose executable path cannot be read is "unconfirmed";
#   - the expected interpreter with an unreadable command line is
#     "unconfirmed";
#   - a different executable (or a readable non-matching run.py) is not Jia;
#   - any query failure is "unconfirmed", never "not found".
function Find-JiaProcess {
    param(
        [string]$ExpectedPython,
        [string]$ExpectedScript,
        [scriptblock]$GetProcesses = $null
    )
    if ([string]::IsNullOrWhiteSpace($ExpectedPython) -or [string]::IsNullOrWhiteSpace($ExpectedScript)) {
        return [pscustomobject]@{ Ok = $false; Pids = @(); Reason = 'expected-identity-missing'; Error = '' }
    }
    if ($null -eq $GetProcesses) {
        $GetProcesses = {
            Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop
        }
    }

    try {
        $processes = @(& $GetProcesses)
    } catch {
        return [pscustomobject]@{
            Ok = $false
            Pids = @()
            Reason = 'process-query-failed'
            Error = $_.Exception.Message
        }
    }

    $pids = New-Object 'System.Collections.Generic.List[int]'
    foreach ($process in $processes) {
        if ($null -eq $process) {
            return [pscustomobject]@{ Ok = $false; Pids = @(); Reason = 'process-entry-unreadable'; Error = '' }
        }
        $processId = 0
        if (-not [int]::TryParse([string]$process.ProcessId, [ref]$processId) -or $processId -le 0) {
            return [pscustomobject]@{ Ok = $false; Pids = @(); Reason = 'process-entry-unreadable'; Error = '' }
        }

        $actualExe = [string]$process.ExecutablePath
        if ([string]::IsNullOrWhiteSpace($actualExe)) {
            # A python.exe with an unreadable path could still be JiaAgent.
            return [pscustomobject]@{ Ok = $false; Pids = @(); Reason = 'process-identity-unreadable'; Error = '' }
        }
        try {
            $sameExe = [string]::Equals(
                [IO.Path]::GetFullPath($actualExe),
                [IO.Path]::GetFullPath($ExpectedPython),
                [StringComparison]::OrdinalIgnoreCase)
        } catch {
            return [pscustomobject]@{ Ok = $false; Pids = @(); Reason = 'process-identity-unreadable'; Error = '' }
        }
        if (-not $sameExe) { continue }

        $info = [pscustomobject]@{
            Pid = $processId
            ExecutablePath = $actualExe
            CommandLine = [string]$process.CommandLine
            StartTimeUtc = $null
        }
        if (Test-JiaProcessIdentity `
            -Info $info `
            -ExpectedPython $ExpectedPython `
            -ExpectedScript $ExpectedScript `
            -ExpectedStartTime $null) {
            $pids.Add($processId)
            continue
        }
        if ([string]::IsNullOrWhiteSpace($info.CommandLine)) {
            # It runs the expected interpreter but we cannot rule out run.py.
            return [pscustomobject]@{ Ok = $false; Pids = @(); Reason = 'process-identity-unreadable'; Error = '' }
        }
    }

    return [pscustomobject]@{ Ok = $true; Pids = $pids.ToArray(); Reason = ''; Error = '' }
}

# Decide what to do when the PID state file is missing. Success (exit 0) is
# allowed only when the strict health check is not reachable, the process scan
# completed, and no process matches JiaAgent's interpreter + run.py.
function Get-JiaNoPidStopOutcome {
    param(
        [bool]$HealthChecked,
        [bool]$HealthOk,
        [string]$HealthError = '',
        [bool]$ScanOk,
        [int[]]$JiaPids = @(),
        [string]$ScanReason = ''
    )
    $pidCount = 0
    if ($null -ne $JiaPids) { $pidCount = @($JiaPids).Count }

    if ($HealthOk) {
        return [pscustomobject]@{
            ExitCode = 1
            Code = 'health-reachable'
            Message = 'JiaAgent health endpoint is still reachable, but no trusted PID state exists. The service may still be running; refusing to guess a PID.'
        }
    }
    if (-not $HealthChecked) {
        $detail = ''
        if (-not [string]::IsNullOrWhiteSpace($HealthError)) { $detail = " ($HealthError)" }
        return [pscustomobject]@{
            ExitCode = 1
            Code = 'health-unchecked'
            Message = "No PID state file, and the health endpoint could not be checked$detail. Cannot confirm JiaAgent is stopped."
        }
    }
    if (-not $ScanOk) {
        $detail = ''
        if (-not [string]::IsNullOrWhiteSpace($ScanReason)) { $detail = " ($ScanReason)" }
        return [pscustomobject]@{
            ExitCode = 1
            Code = 'scan-unconfirmed'
            Message = "No PID state file and health is not reachable; the process scan was inconclusive$detail. Cannot confirm no JiaAgent instance is running."
        }
    }
    if ($pidCount -gt 0) {
        return [pscustomobject]@{
            ExitCode = 1
            Code = 'jia-process-found'
            Message = "No PID state file, but process(es) matching JiaAgent identity are running (PID $($JiaPids -join ', ')). Refusing to stop without trusted PID state."
        }
    }
    return [pscustomobject]@{
        ExitCode = 0
        Code = 'no-jia-found'
        Message = 'No PID state file; health endpoint is not reachable; no matching JiaAgent process was found. JiaAgent appears to be stopped.'
    }
}

function Remove-JiaPidStateIfOwned {
    param([string]$Path, [int]$PidValue, $StartedAt = $null)
    $state = Read-JiaPidState -Path $Path
    if ($null -eq $state -or $state.Legacy -or $state.Pid -ne $PidValue) { return $false }
    if ($null -ne $StartedAt -and -not [string]::IsNullOrWhiteSpace([string]$StartedAt)) {
        if ($null -eq $state.StartedAt) { return $false }
        if ([math]::Abs([double]$state.StartedAt - [double]$StartedAt) -gt 0.001) { return $false }
    }
    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    return (-not (Test-Path -LiteralPath $Path))
}
