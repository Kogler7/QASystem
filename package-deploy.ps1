param(
    [switch]$IncludeImage,
    [string]$ImageName = "qasystem-backend:latest",
    [string]$RedisImage = "redis:7-alpine",
    [switch]$SkipImageVerification
)

$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
$deployDir = Join-Path $repoRoot "deploy"
$sourceMountedDir = Join-Path $deployDir "source-mounted"
$runtimeDir = Join-Path $sourceMountedDir "runtime"
$legacyRuntimeDir = Join-Path $deployDir "qasystem-runtime"
$resolvedRepoRoot = [System.IO.Path]::GetFullPath($repoRoot)
$resolvedDeployDir = [System.IO.Path]::GetFullPath($deployDir)

if (-not $resolvedDeployDir.StartsWith($resolvedRepoRoot + [System.IO.Path]::DirectorySeparatorChar)) {
    throw "Deployment directory must stay inside the repository: $resolvedDeployDir"
}

New-Item -ItemType Directory -Force -Path $deployDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $deployDir "models") | Out-Null
New-Item -ItemType Directory -Force -Path $sourceMountedDir | Out-Null

foreach ($tempFile in Get-ChildItem -LiteralPath $deployDir -Filter ".tmp-*" -File -ErrorAction SilentlyContinue) {
    Remove-Item -LiteralPath $tempFile.FullName -Force
}

if (Test-Path -LiteralPath $legacyRuntimeDir) {
    Remove-Item -LiteralPath $legacyRuntimeDir -Recurse -Force
}
if (Test-Path -LiteralPath $runtimeDir) {
    Remove-Item -LiteralPath $runtimeDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

foreach ($directory in @("backend", "algorithm", "agent", "mcp_services", "ofd_converter")) {
    $source = Join-Path $repoRoot $directory
    $destination = Join-Path $runtimeDir $directory
    robocopy $source $destination /E /NFL /NDL /NJH /NJS /NP /XD __pycache__ .venv venv env .pytest_cache .mypy_cache .ruff_cache /XF .env ".env.*" "*.pyc" "*.pyo" "*.log"
    if ($LASTEXITCODE -ge 8) {
        throw "Failed to copy $directory (robocopy exit code $LASTEXITCODE)"
    }
}

Copy-Item -LiteralPath (Join-Path $repoRoot "docker-compose.yml") -Destination (Join-Path $deployDir "docker-compose.yml") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "docker\DEPLOY_PACKAGE_README.md") -Destination (Join-Path $deployDir "README.md") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "docker\docker-compose.delivery.yml") -Destination (Join-Path $sourceMountedDir "docker-compose.yml") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "docker\DEPLOY_SOURCE_MOUNTED_README.md") -Destination (Join-Path $sourceMountedDir "README.md") -Force

$envTemplate = Get-Content -LiteralPath (Join-Path $repoRoot ".env.example") -Raw
$selfContainedEnv = $envTemplate -replace "(?m)^HOST_MODELS_DIR=.*$", "HOST_MODELS_DIR=./models"
$sourceMountedEnv = $envTemplate -replace "(?m)^HOST_MODELS_DIR=.*$", "HOST_MODELS_DIR=../models"
Set-Content -LiteralPath (Join-Path $deployDir ".env.example") -Value $selfContainedEnv -Encoding utf8
Set-Content -LiteralPath (Join-Path $sourceMountedDir ".env.example") -Value $sourceMountedEnv -Encoding utf8

$images = @()
if ($IncludeImage) {
    docker image inspect $ImageName | Out-Null
    if (-not $SkipImageVerification) {
        docker run --rm --entrypoint python $ImageName /usr/local/bin/verify_qasystem_runtime.py
        if ($LASTEXITCODE -ne 0) {
            throw "Image verification failed (exit code $LASTEXITCODE): $ImageName"
        }
    }

    docker image inspect $RedisImage *> $null
    if ($LASTEXITCODE -ne 0) {
        docker pull $RedisImage
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to pull Redis image: $RedisImage"
        }
    }

    $backendTar = Join-Path $deployDir "qasystem-backend-latest.tar"
    $redisTar = Join-Path $deployDir "redis-7-alpine.tar"
    docker save -o $backendTar $ImageName
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to export image: $ImageName"
    }
    docker save -o $redisTar $RedisImage
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to export image: $RedisImage"
    }

    foreach ($item in @(
        @{ tag = $ImageName; file = $backendTar },
        @{ tag = $RedisImage; file = $redisTar }
    )) {
        $images += [ordered]@{
            tag = $item.tag
            image_id = (docker image inspect $item.tag --format "{{.Id}}").Trim()
            archive = [System.IO.Path]::GetFileName($item.file)
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $item.file).Hash.ToLowerInvariant()
        }
    }
}

$manifest = [ordered]@{
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    git_commit = (git -C $repoRoot rev-parse HEAD 2>$null)
    layout = "self-contained-with-source-mounted-alternative"
    source_mounted_runtime = "source-mounted/runtime/"
    variants = @("self-contained", "source-mounted")
    images = $images
    files = [ordered]@{}
}
foreach ($file in @("docker-compose.yml", ".env.example", "README.md", "source-mounted/docker-compose.yml", "source-mounted/.env.example", "source-mounted/README.md")) {
    $path = Join-Path $deployDir $file
    $manifest.files[$file] = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $deployDir "交付清单.json") -Encoding utf8

Write-Host "Deployment bundle generated at $deployDir"
Write-Host "Use deploy/ for the self-contained mode, or deploy/source-mounted/ for the external-source mode."
