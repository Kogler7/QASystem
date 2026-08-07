param(
    [string]$BaseImage = "qasystem-base-v1",
    [string]$RuntimeImage = "qasystem-runtime-v1",
    [switch]$BuildBase,
    [switch]$BaseOnly,
    [string]$OpenCodeVersion = "1.17.15",
    [string]$NodeImage = "node:20-bullseye-slim",
    [string]$MavenImage = "maven:3.9.9-eclipse-temurin-17",
    [string]$VllmImage = "vllm/vllm-openai:v0.11.2",
    [switch]$NoCache,
    [switch]$KeepCandidate
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot

function Write-ProgressLog([string]$Message) {
    Write-Host "[$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))] $Message"
}

function Invoke-Docker([string[]]$Arguments, [string]$FailureMessage) {
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit code $LASTEXITCODE)"
    }
}

function Test-DockerImage([string]$Image) {
    docker image inspect $Image *> $null
    return $LASTEXITCODE -eq 0
}

function Build-And-Promote(
    [string]$Dockerfile,
    [string]$TargetImage,
    [string[]]$BuildArguments,
    [scriptblock]$Verify
) {
    $candidate = "${TargetImage}-candidate"
    $arguments = @("build", "--progress=plain", "--file", $Dockerfile, "--tag", $candidate)
    foreach ($item in $BuildArguments) {
        $arguments += @("--build-arg", $item)
    }
    if ($NoCache) { $arguments += "--no-cache" }
    $arguments += "."

    Write-ProgressLog "Building candidate $candidate from $Dockerfile"
    Invoke-Docker $arguments "Docker build failed for $candidate"
    Write-ProgressLog "Verifying candidate $candidate"
    & $Verify $candidate
    Invoke-Docker @("tag", $candidate, $TargetImage) "Failed to promote $candidate"
    $imageId = (docker image inspect $TargetImage --format "{{.Id}}").Trim()
    if (-not $KeepCandidate) {
        docker image rm $candidate *> $null
    }
    Write-ProgressLog "Promoted $TargetImage ($imageId)"
}

Push-Location $repoRoot
try {
    if ($BuildBase) {
        Build-And-Promote "Dockerfile" $BaseImage @(
            "OPENCODE_VERSION=$OpenCodeVersion",
            "NODE_IMAGE=$NodeImage",
            "MAVEN_IMAGE=$MavenImage",
            "VLLM_IMAGE=$VllmImage"
        ) {
            param($candidate)
            Invoke-Docker @(
                "run", "--rm", "--entrypoint", "/bin/sh", $candidate, "-c",
                "python -c 'import fastapi,torch,vllm,cv2,redis,mcp,openai' && test -x /usr/bin/soffice && test -f /opt/qasystem/ofdrw/ofd-to-pdf.jar && command -v tini >/dev/null"
            ) "Dependency-base verification failed"
        }
    }
    elseif (-not (Test-DockerImage $BaseImage)) {
        throw "Dependency base not found: $BaseImage. Build it once with .\build-image.ps1 -BuildBase, or create it from the legacy image with docker/make-runtime-base.sh."
    }

    if ($BaseOnly) {
        if (-not $BuildBase) { throw "-BaseOnly requires -BuildBase" }
        Write-ProgressLog "Base-only build completed; runtime image was not rebuilt"
        return
    }

    Build-And-Promote "Dockerfile.runtime" $RuntimeImage @("BASE_IMAGE=$BaseImage") {
        param($candidate)
        Invoke-Docker @("run", "--rm", "--entrypoint", "python", $candidate, "/usr/local/bin/verify_qasystem_runtime.py") "Runtime verification failed"
    }
    Write-ProgressLog "Done. Normal code releases only rebuild $RuntimeImage; use -BuildBase only when the dependency contract changes."
}
finally {
    Pop-Location
}
