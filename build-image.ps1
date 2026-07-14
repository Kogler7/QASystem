param(
    [string]$ImageName = "qasystem-backend:latest",
    [string]$OpenCodeVersion = "1.17.15",
    [string]$PythonImage = "python:3.12-bookworm",
    [string]$NodeImage = "node:20-bullseye-slim",
    [switch]$NoCache,
    [switch]$KeepCandidate
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot

if ($ImageName -notmatch "^(?<repository>.+?)(?::(?<tag>[^/:]+))?$") {
    throw "Invalid image name: $ImageName"
}
$repository = $Matches.repository
$tag = if ($Matches.tag) { $Matches.tag } else { "latest" }
$candidateImage = "${repository}:candidate-${tag}"

$oldImageId = $null
docker image inspect $ImageName *> $null
if ($LASTEXITCODE -eq 0) {
    $oldImageId = (docker image inspect $ImageName --format "{{.Id}}").Trim()
}

$buildArgs = @(
    "build",
    "--progress=plain",
    "--build-arg", "OPENCODE_VERSION=$OpenCodeVersion",
    "--build-arg", "PYTHON_IMAGE=$PythonImage",
    "--build-arg", "NODE_IMAGE=$NodeImage",
    "--tag", $candidateImage
)
if ($NoCache) {
    $buildArgs += "--no-cache"
}
$buildArgs += "."

Push-Location $repoRoot
try {
    & docker @buildArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Docker build failed with exit code $LASTEXITCODE"
    }

    docker run --rm --entrypoint python $candidateImage /usr/local/bin/verify_qasystem_runtime.py
    if ($LASTEXITCODE -ne 0) {
        throw "Candidate image verification failed with exit code $LASTEXITCODE"
    }

    docker tag $candidateImage $ImageName
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to promote candidate image to $ImageName"
    }

    $newImageId = (docker image inspect $ImageName --format "{{.Id}}").Trim()
    if (-not $KeepCandidate) {
        docker image rm $candidateImage | Out-Null
    }

    if ($oldImageId -and $oldImageId -ne $newImageId) {
        docker image rm $oldImageId *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "The old image is still referenced by another tag or child image: $oldImageId"
        }
    }

    Write-Host "Verified image promoted: $ImageName ($newImageId)"
}
finally {
    Pop-Location
}
