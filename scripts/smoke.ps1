<#
.SYNOPSIS
  Smoke test against a running OmpAgentFramework instance.
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8710',
    [string]$ScopeId = 'prod-cluster-01',
    [int]$TimeoutSec = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$endpoints = @(
    '/api/v1/health',
    "/api/v1/launchpad?scope_id=$ScopeId",
    '/api/v1/agents',
    '/api/v1/tools',
    '/api/v1/intents',
    '/api/v1/tickets',
    '/api/v1/rules',
    '/api/v1/triggers',
    '/api/v1/memory',
    '/api/v1/audit'
)

$failed = 0
foreach ($ep in $endpoints) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $res = Invoke-WebRequest -Uri "$BaseUrl$ep" -UseBasicParsing -TimeoutSec $TimeoutSec
        $sw.Stop()
        Write-Host ("OK    {0}  ({1} ms)  {2}" -f $res.StatusCode, $sw.ElapsedMilliseconds, $ep)
    }
    catch {
        $sw.Stop()
        $failed++
        Write-Host ("FAIL  ---  ({0} ms)  {1} : {2}" -f $sw.ElapsedMilliseconds, $ep, $_.Exception.Message)
    }
}

if ($failed -gt 0) {
    Write-Host "[smoke] FAILED ($failed endpoint(s))"
    exit 1
}
Write-Host "[smoke] ALL PASSED"
exit 0
