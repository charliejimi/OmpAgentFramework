<#
.SYNOPSIS
  Start the OmpAgentFramework API server and Launchpad UI.
#>
[CmdletBinding()]
param(
    [int]$Port = 8710,
    [switch]$SkipSync,
    [switch]$NoBrowser,
    [int]$StopAfterSec = -1
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not $SkipSync) {
    Write-Host "[dev] Syncing Python dependencies (uv sync --extra api)..."
    & uv sync --extra api
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

$OmpExe = Join-Path $RepoRoot 'tools\omp\omp.exe'
if (-not (Test-Path -LiteralPath $OmpExe)) {
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'get-omp.ps1')
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

$env:OMP_API_PORT = "$Port"
$PythonExe = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$Url = "http://127.0.0.1:$Port"

Write-Host "[dev] Starting OmpAgentFramework at $Url ..."
$proc = Start-Process -FilePath $PythonExe -ArgumentList @(
    '-m', 'uvicorn', 'omp_framework.api.app:create_app', '--factory',
    '--host', '127.0.0.1', '--port', "$Port", '--timeout-graceful-shutdown', '3'
) -WorkingDirectory $RepoRoot -PassThru -NoNewWindow

try {
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        if ($proc.HasExited) {
            Write-Error "[dev] Server exited prematurely with code $($proc.ExitCode)"
            exit 1
        }
        try {
            $resp = Invoke-RestMethod -Uri "$Url/api/v1/health" -TimeoutSec 2
            if ($resp.data.status -in @('ok', 'degraded')) {
                $ready = $true
                Write-Host "[dev] Ready! status=$($resp.data.status), omp=$($resp.data.omp.status) ($($resp.data.omp.version)), domain=$($resp.data.domain)"
                break
            }
        } catch {}
        Start-Sleep -Seconds 1
    }
    if (-not $ready) {
        Write-Error "[dev] Timed out waiting for $Url/api/v1/health"
        exit 1
    }
    if (-not $NoBrowser) {
        Start-Process "$Url/"
    }
    if ($StopAfterSec -ge 0) {
        Start-Sleep -Seconds $StopAfterSec
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        exit 0
    }
    $proc.WaitForExit()
    exit $proc.ExitCode
}
finally {
    if (-not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
}
