param(
    [string]$ImageName = "qasystem-env-v1",
    [string]$OpenCodeVersion = "1.17.15",
    [string]$NodeImage = "node:20-bullseye-slim",
    [string]$MavenImage = "maven:3.9.9-eclipse-temurin-17",
    [string]$VllmImage = "vllm/vllm-openai:v0.11.2",
    [switch]$NoCache,
    [switch]$KeepCandidate
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$candidateImage = "${ImageName}-candidate"

function Write-ProgressLog([string]$Message) {
    Write-Host "[$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))] $Message"
}

function Invoke-Docker([string[]]$Arguments, [string]$FailureMessage) {
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$FailureMessage (exit code $LASTEXITCODE)" }
}

Push-Location $repoRoot
try {
    $buildArguments = @("build")
    if ($env:DOCKER_BUILDKIT -ne "0") { $buildArguments += "--progress=plain" }
    $buildArguments += @(
        "--file", "Dockerfile", "--tag", $candidateImage,
        "--build-arg", "OPENCODE_VERSION=$OpenCodeVersion",
        "--build-arg", "NODE_IMAGE=$NodeImage",
        "--build-arg", "MAVEN_IMAGE=$MavenImage",
        "--build-arg", "VLLM_IMAGE=$VllmImage"
    )
    if ($NoCache) { $buildArguments += "--no-cache" }
    $buildArguments += "."

    Write-ProgressLog "[1/4] Building source-free environment image $candidateImage"
    Invoke-Docker $buildArguments "Environment image build failed"

    Write-ProgressLog "[2/4] Verifying dependencies with the repository source mounted read-only"
    $verifyArguments = @("run", "--rm", "--entrypoint", "python")
    foreach ($directory in @("backend", "algorithm", "mcp_services", "agent", "docker")) {
        $source = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $directory))
        $verifyArguments += @("--mount", "type=bind,source=$source,target=/app/$directory,readonly")
    }
    $verifyArguments += @(
        "--env", "PYTHONPATH=/app:/app/agent",
        "--env", "LIBREOFFICE_PATH=/usr/bin/soffice",
        "--env", "OFD_CONVERTER_COMMAND=java -jar /opt/qasystem/ofdrw/ofd-to-pdf.jar",
        $candidateImage,
        "/app/docker/verify_runtime.py"
    )
    Invoke-Docker $verifyArguments "Mounted-source runtime verification failed"

    Write-ProgressLog "[3/4] Promoting verified image to $ImageName"
    Invoke-Docker @("tag", $candidateImage, $ImageName) "Failed to promote environment image"
    if (-not $KeepCandidate) { docker image rm $candidateImage *> $null }

    $imageId = (docker image inspect $ImageName --format "{{.Id}}").Trim()
    Write-ProgressLog "[4/4] Environment image ready: $ImageName ($imageId)"
    Write-ProgressLog "Application source and models are not included; Compose loads them from host mounts."
}
finally {
    Pop-Location
}
