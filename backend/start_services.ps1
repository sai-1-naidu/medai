$ErrorActionPreference = "Stop"

$BackendDir = $PSScriptRoot
Set-Location $BackendDir
$DashboardPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
$PipecatPython = Join-Path $BackendDir ".venv-pipecat\Scripts\python.exe"

if (-not (Test-Path $DashboardPython)) {
    throw "Backend environment is missing. Create backend\.venv and install requirements-dev.txt first."
}
if (-not (Test-Path $PipecatPython)) {
    throw "Pipecat environment is missing. Create backend\.venv-pipecat and install requirements-pipecat.txt first."
}

$configSource = @'
import json
from app.config import (
    DASHBOARD_HOST, DASHBOARD_PORT, DASHBOARD_PROBE_HOST,
    PIPECAT_HOST, PIPECAT_PORT, PIPECAT_PROBE_HOST, PIPECAT_PROBE_URL,
    GROQ_API_KEY, ELEVENLABS_API_KEY,
)
print(json.dumps({
    "dashboard_host": DASHBOARD_HOST,
    "dashboard_port": DASHBOARD_PORT,
    "dashboard_probe_host": DASHBOARD_PROBE_HOST,
    "pipecat_host": PIPECAT_HOST,
    "pipecat_port": PIPECAT_PORT,
    "pipecat_probe_host": PIPECAT_PROBE_HOST,
    "pipecat_probe_url": PIPECAT_PROBE_URL,
    "missing_voice_keys": [
        name for name, value in (
            ("GROQ_API_KEY", GROQ_API_KEY),
            ("ELEVENLABS_API_KEY", ELEVENLABS_API_KEY),
        ) if not value
    ],
}))
'@
$configJson = $configSource | & $DashboardPython -
if ($LASTEXITCODE -ne 0) {
    throw "Could not load service configuration. Check backend\.env and the Python environment."
}
$config = $configJson | ConvertFrom-Json

function Test-ListeningPort {
    param([int]$Port)
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function Test-HttpEndpoint {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
        return $response.StatusCode -eq 200
    }
    catch {
        if ($_.Exception.Response -or $_.Exception -is [System.Net.WebException]) {
            return $false
        }
        throw
    }
}

function Wait-ForEndpoint {
    param(
        [string]$Url,
        [System.Diagnostics.Process]$Process,
        [string]$ServiceName
    )
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if ($Process.HasExited) {
            throw "$ServiceName exited before becoming ready. Inspect its service window for the startup error."
        }
        if (Test-HttpEndpoint $Url) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$ServiceName did not become ready within 30 seconds."
}

$dashboardUrl = "http://$($config.dashboard_probe_host):$($config.dashboard_port)/health"
$voiceUrl = $config.pipecat_probe_url

if (Test-HttpEndpoint $dashboardUrl) {
    Write-Host "Dashboard backend already healthy; not starting a duplicate."
}
elseif (Test-ListeningPort ([int]$config.dashboard_port)) {
    throw "Port $($config.dashboard_port) is occupied, but the MediVoice health check failed. Resolve that listener before starting another backend."
}
else {
    $dashboardArgs = @(
        "-m", "uvicorn", "app.main:app",
        "--host", [string]$config.dashboard_host,
        "--port", [string]$config.dashboard_port
    )
    $dashboardProcess = Start-Process `
        -FilePath $DashboardPython `
        -ArgumentList $dashboardArgs `
        -WorkingDirectory $BackendDir `
        -PassThru
    Write-Host "Started dashboard backend (PID $($dashboardProcess.Id))."
    Wait-ForEndpoint $dashboardUrl $dashboardProcess "Dashboard backend"
}

if (Test-HttpEndpoint $voiceUrl) {
    Write-Host "Pipecat voice runner already healthy; not starting a duplicate."
}
elseif (Test-ListeningPort ([int]$config.pipecat_port)) {
    throw "Port $($config.pipecat_port) is occupied, but the Pipecat client health check failed. Resolve that listener before starting another voice runner."
}
else {
    if ($config.missing_voice_keys.Count -gt 0) {
        $missing = $config.missing_voice_keys -join ", "
        throw "The Pipecat voice runner needs these values in backend\.env: $missing. No credential values are displayed."
    }

    $voiceArgs = @(
        "bot.py", "-t", "webrtc",
        "--host", [string]$config.pipecat_host,
        "--port", [string]$config.pipecat_port
    )
    $voiceProcess = Start-Process `
        -FilePath $PipecatPython `
        -ArgumentList $voiceArgs `
        -WorkingDirectory $BackendDir `
        -PassThru
    Write-Host "Started Pipecat WebRTC runner (PID $($voiceProcess.Id))."
    Wait-ForEndpoint $voiceUrl $voiceProcess "Pipecat WebRTC runner"
}

Write-Host "Dashboard: http://$($config.dashboard_probe_host):$($config.dashboard_port)"
Write-Host "Pipecat client: $voiceUrl"
Write-Host "Keep both service windows open. Stop each process with Ctrl+C in its service window."
