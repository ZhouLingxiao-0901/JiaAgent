# Launch JiaAgent from this project's virtual environment.
#
# Intended to be started by Task Scheduler "At log on" for the *current user*
# with limited privileges. It does not request elevation, does not change any
# WeChat allowlist/scope, and never prints credentials.
#
# Exit codes:
#   0    JiaAgent exited normally (or another instance was already running)
#   2    project files / virtualenv missing
#   *    whatever run.py returned
[CmdletBinding()]
param(
    # Opt in to the WeChat listener for this launch. The listener still fails
    # closed without DPAPI credentials and an explicit pairing, and its scopes
    # remain exactly what the allowlist says (never granted here).
    [switch]$EnableWeChat
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$LogDir = Join-Path $Root 'logs'
$LogPath = Join-Path $LogDir 'autostart.log'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$RunScript = Join-Path $Root 'run.py'
$RedactScript = Join-Path $Root 'scripts\run_jia_redacted.py'
$MaxLogBytes = 5MB

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-AutostartLog {
    param([string]$Message)
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    # Logging must never take Jia down (for example on a full disk).
    Add-Content -LiteralPath $LogPath -Value "[$stamp] $Message" `
        -Encoding utf8 -ErrorAction SilentlyContinue
}

function Invoke-LogRotation {
    try {
        if ((Test-Path -LiteralPath $LogPath) -and
            ((Get-Item -LiteralPath $LogPath).Length -gt $MaxLogBytes)) {
            Move-Item -LiteralPath $LogPath -Destination ($LogPath + '.1') -Force
        }
    } catch {
        # A log rotation failure must not stop Jia from starting.
    }
}

Invoke-LogRotation

if (-not (Test-Path -LiteralPath $Python)) {
    Write-AutostartLog "FAILED: virtualenv python not found: $Python"
    exit 2
}
if (-not (Test-Path -LiteralPath $RunScript)) {
    Write-AutostartLog "FAILED: run.py not found: $RunScript"
    exit 2
}
if (-not (Test-Path -LiteralPath $RedactScript)) {
    Write-AutostartLog "FAILED: redacting launcher not found: $RedactScript"
    exit 2
}

if ($EnableWeChat) {
    $env:JIA_WECHAT_ENABLED = '1'
    Write-AutostartLog 'WeChat listener requested for this launch (-EnableWeChat).'
} else {
    Write-AutostartLog 'WeChat listener follows .env / process environment (no override).'
}

# Unbuffered so startup failures reach autostart.log immediately.
$env:PYTHONUNBUFFERED = '1'
# Keep Python's piped stdout/stderr UTF-8, and decode it the same way.
# Without this, Chinese log lines can be garbled in autostart.log on a
# Chinese Windows install.
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

Write-AutostartLog "Starting JiaAgent (python=$Python, cwd=$Root)."

$exitCode = 0
$lineCount = 0
Push-Location -Path $Root
try {
    # Merge stderr inside cmd, not in PowerShell. In Windows PowerShell 5.1,
    # `native.exe 2>&1` wraps each stderr line in an ErrorRecord; with
    # $ErrorActionPreference = 'Stop' the first such line (uvicorn logs to
    # stderr) terminates the pipeline and can take the launcher down.
    #
    # run_jia_redacted.py starts run.py and filters its output before it
    # reaches this log; run.py is still the process recorded in the PID file.
    $cmdLine = '""{0}" "{1}" 2>&1"' -f $Python, $RedactScript
    & cmd.exe /d /c $cmdLine | ForEach-Object {
        $lineCount++
        if (($lineCount % 200) -eq 0) {
            Invoke-LogRotation
        }
        $stamp = Get-Date -Format 'HH:mm:ss'
        Add-Content -LiteralPath $LogPath -Value "[$stamp] $_" `
            -Encoding utf8 -ErrorAction SilentlyContinue
        # Visible when started manually; harmless in the hidden task window.
        Write-Host $_
    }
    if ($null -ne $LASTEXITCODE) {
        $exitCode = [int]$LASTEXITCODE
    }
} finally {
    Pop-Location
}

Invoke-LogRotation
Write-AutostartLog "JiaAgent exited with code $exitCode."
exit $exitCode
