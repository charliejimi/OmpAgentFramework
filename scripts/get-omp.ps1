<#
.SYNOPSIS
  Fetch omp 18.11.2 (oh-my-pi) prebuilt Windows binary and verify SHA-256.
#>
[CmdletBinding()]
param(
    [string]$CachePath = (Join-Path $env:TEMP 'hs_spike\omp.exe')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$OmpVersion = '18.11.2'
$OmpSha256 = '97f5c9d534141cd6c4fc8abe85818de550772ce397da08623b0aa25d9f944946'
$BuildHost = 'https://build.stencil.so'
$BuildApi = "$BuildHost/api/products/omp/versions/$OmpVersion/windows-x86_64"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$OmpDir = Join-Path $RepoRoot 'tools\omp'
$OmpExe = Join-Path $OmpDir 'omp.exe'
$PartialExe = Join-Path $OmpDir 'omp.exe.partial'

function Write-Step([string]$Message) {
    Write-Host "[get-omp] $Message"
}

function Stop-WithError([string]$Message) {
    [Console]::Error.WriteLine("[get-omp] ERROR: $Message")
    exit 1
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

if (-not (Test-Path -LiteralPath $OmpDir)) {
    New-Item -ItemType Directory -Force -Path $OmpDir | Out-Null
}

if (Test-Path -LiteralPath $OmpExe -PathType Leaf) {
    $current = Get-Sha256 $OmpExe
    if ($current -eq $OmpSha256) {
        Write-Step "tools/omp/omp.exe already exists and SHA-256 matches ($OmpVersion)."
        exit 0
    }
    Write-Step "tools/omp/omp.exe SHA-256 mismatch ($current), re-acquiring..."
}

$copied = $false
if (Test-Path -LiteralPath $CachePath -PathType Leaf) {
    if ((Get-Sha256 $CachePath) -eq $OmpSha256) {
        Write-Step "Copying from local cache: $CachePath"
        Copy-Item -LiteralPath $CachePath -Destination $PartialExe -Force
        $copied = $true
    }
}

if (-not $copied) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Write-Step "Querying build metadata from $BuildApi"
    $meta = Invoke-RestMethod -Uri $BuildApi -UseBasicParsing -TimeoutSec 60
    $url = $meta.download
    if (-not $url -and $meta.file.url) {
        $url = "$BuildHost$($meta.file.url)"
    }
    if ($url.StartsWith('/')) { $url = "$BuildHost$url" }
    Write-Step "Downloading omp $OmpVersion from $url ..."
    Invoke-WebRequest -Uri $url -OutFile $PartialExe -UseBasicParsing -TimeoutSec 600
}

$gotHash = Get-Sha256 $PartialExe
if ($gotHash -ne $OmpSha256) {
    Remove-Item -LiteralPath $PartialExe -Force -ErrorAction SilentlyContinue
    Stop-WithError "SHA-256 verification failed: got $gotHash, expected $OmpSha256"
}

Move-Item -LiteralPath $PartialExe -Destination $OmpExe -Force
Write-Step "omp $OmpVersion ready at tools/omp/omp.exe (SHA-256 verified)."
