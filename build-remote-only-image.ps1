param(
    [string]$ImageName = "qasystem-env-v1-remote-only",
    [string]$PythonImage = "python:3.12-bookworm",
    [switch]$NoCache
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot

function Write-ProgressLog([string]$Message) {
    Write-Host "[$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))] $Message"
}

function Invoke-Docker([string[]]$Arguments, [string]$FailureMessage) {
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$FailureMessage (exit code $LASTEXITCODE)" }
}

Push-Location $repoRoot
try {
    $candidate = "${ImageName}-candidate"
    $arguments = @(
        "build", "--file", "Dockerfile.remote-only", "--tag", $candidate,
        "--build-arg", "PYTHON_IMAGE=$PythonImage"
    )
    if ($env:DOCKER_BUILDKIT -ne "0") { $arguments += "--progress=plain" }
    if ($NoCache) { $arguments += "--no-cache" }
    $arguments += "."

    Write-ProgressLog "[1/4] Building source-free remote-only image $candidate"
    Invoke-Docker $arguments "Remote-only image build failed"

    Write-ProgressLog "[2/4] Verifying remote-only dependencies with mounted source"
    $verify = @("run", "--rm", "--entrypoint", "python")
    foreach ($directory in @("backend", "algorithm", "mcp_services", "agent", "external_services")) {
        $source = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $directory))
        $verify += @("--mount", "type=bind,source=$source,target=/app/$directory,readonly")
    }
    $verify += @(
        "--env", "PYTHONPATH=/app:/app/agent",
        "--env", "ALGORITHM_RUNTIME_PROFILE=remote-only",
        $candidate,
        "-c",
        "import fastapi, httpx, mcp, openai, redis, uvicorn; from algorithm.runtime_profile import is_remote_only; assert is_remote_only(); print('remote-only runtime verified')"
    )
    Invoke-Docker $verify "Mounted-source remote-only verification failed"

    Write-ProgressLog "[3/4] Promoting verified image to $ImageName"
    Invoke-Docker @("tag", $candidate, $ImageName) "Failed to promote remote-only image"
    docker image rm $candidate *> $null

    $imageId = (docker image inspect $ImageName --format "{{.Id}}").Trim()
    $size = (docker image inspect $ImageName --format "{{.Size}}").Trim()
    Write-ProgressLog "[4/4] Remote-only image ready: $ImageName ($imageId, $size bytes)"
    Write-ProgressLog "No application source or model weights are included."
}
finally {
    Pop-Location
}
