param(
    [switch]$IncludeImage,
    [string]$ImageName = "qasystem-backend:latest",
    [string]$RedisImage = "redis:7-alpine",
    [switch]$SkipImageVerification,
    [switch]$ReuseExistingImages
)

$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
$deployDir = Join-Path $repoRoot "deploy"
$obsoleteSourceMountedDir = Join-Path $deployDir "source-mounted"
$offlineDir = Join-Path $deployDir "offline-validation"
$legacyRuntimeDir = Join-Path $deployDir "qasystem-runtime"
$resolvedRepoRoot = [System.IO.Path]::GetFullPath($repoRoot)
$resolvedDeployDir = [System.IO.Path]::GetFullPath($deployDir)

function Get-Sha256([string]$Path) {
    $output = & certutil.exe -hashfile $Path SHA256
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to calculate SHA-256: $Path"
    }
    $hashLine = $output | Where-Object { $_ -match '^[0-9A-Fa-f ]+$' } | Select-Object -First 1
    if (-not $hashLine) {
        throw "Unable to parse SHA-256 output: $Path"
    }
    return ($hashLine -replace '\s', '').ToLowerInvariant()
}

if (-not $resolvedDeployDir.StartsWith($resolvedRepoRoot + [System.IO.Path]::DirectorySeparatorChar)) {
    throw "Deployment directory must stay inside the repository: $resolvedDeployDir"
}

New-Item -ItemType Directory -Force -Path $deployDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $deployDir "models") | Out-Null
New-Item -ItemType Directory -Force -Path $offlineDir | Out-Null

foreach ($tempFile in Get-ChildItem -LiteralPath $deployDir -Filter ".tmp-*" -File -ErrorAction SilentlyContinue) {
    Remove-Item -LiteralPath $tempFile.FullName -Force
}

if (Test-Path -LiteralPath $legacyRuntimeDir) {
    Remove-Item -LiteralPath $legacyRuntimeDir -Recurse -Force
}
if (Test-Path -LiteralPath $obsoleteSourceMountedDir) {
    Remove-Item -LiteralPath $obsoleteSourceMountedDir -Recurse -Force
}

Copy-Item -LiteralPath (Join-Path $repoRoot "docker-compose.yml") -Destination (Join-Path $deployDir "docker-compose.yml") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "download-models.ps1") -Destination (Join-Path $deployDir "download-models.ps1") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "docker\ENV_MIGRATION.md") -Destination (Join-Path $deployDir "ENV_MIGRATION.md") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "run-offline-validation.ps1") -Destination (Join-Path $deployDir "run-offline-validation.ps1") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "docker\docker-compose.offline-test.yml") -Destination (Join-Path $offlineDir "docker-compose.offline-test.yml") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "docker\OFFLINE_VALIDATION.md") -Destination (Join-Path $offlineDir "README.md") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "test_artifacts\parser") -Destination (Join-Path $offlineDir "parser") -Recurse -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "docker\DEPLOY_PACKAGE_README.md") -Destination (Join-Path $deployDir "README.md") -Force
$envTemplate = Get-Content -LiteralPath (Join-Path $repoRoot ".env.example") -Raw
$selfContainedEnv = $envTemplate -replace "(?m)^HOST_MODELS_DIR=.*$", "HOST_MODELS_DIR=./models"
Set-Content -LiteralPath (Join-Path $deployDir ".env.example") -Value $selfContainedEnv -Encoding utf8

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
    if ($ReuseExistingImages) {
        foreach ($archive in @($backendTar, $redisTar)) {
            if (-not (Test-Path -LiteralPath $archive) -or (Get-Item -LiteralPath $archive).Length -eq 0) {
                throw "Cannot reuse missing image archive: $archive"
            }
        }
    }
    else {
        docker save -o $backendTar $ImageName
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to export image: $ImageName"
        }
        docker save -o $redisTar $RedisImage
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to export image: $RedisImage"
        }
    }

    foreach ($item in @(
        @{ tag = $ImageName; file = $backendTar },
        @{ tag = $RedisImage; file = $redisTar }
    )) {
        $images += [ordered]@{
            tag = $item.tag
            image_id = (docker image inspect $item.tag --format "{{.Id}}").Trim()
            archive = [System.IO.Path]::GetFileName($item.file)
            sha256 = Get-Sha256 $item.file
        }
    }
}

$manifest = [ordered]@{
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    git_commit = (git -C $repoRoot rev-parse HEAD 2>$null)
    layout = "self-contained-image"
    variants = @("self-contained")
    images = $images
    files = [ordered]@{}
}
foreach ($file in @("docker-compose.yml", "download-models.ps1", "ENV_MIGRATION.md", "run-offline-validation.ps1", ".env.example", "README.md", "offline-validation/docker-compose.offline-test.yml", "offline-validation/README.md")) {
    $path = Join-Path $deployDir $file
    $manifest.files[$file] = Get-Sha256 $path
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $deployDir "交付清单.json") -Encoding utf8

Write-Host "Deployment bundle generated at $deployDir"
Write-Host "The bundle uses the self-contained QASystem image; no host source mount is required."
