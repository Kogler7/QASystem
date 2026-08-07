param(
    [switch]$IncludeBaseImage,
    [switch]$IncludeRedisImage,
    [string]$BaseImage = "qasystem-base-v1",
    [string]$RuntimeImage = "qasystem-runtime-v1",
    [string]$RedisImage = "redis:7-alpine",
    [switch]$ReuseExistingArchives
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$deployDir = Join-Path $repoRoot "deploy"
$runtimeStage = Join-Path $deployDir ".tmp-runtime-context"
$runtimeArchive = Join-Path $deployDir "runtime-context.tar"
$resolvedRepoRoot = [System.IO.Path]::GetFullPath($repoRoot)
$resolvedDeployDir = [System.IO.Path]::GetFullPath($deployDir)

function Write-ProgressLog([string]$Message) {
    Write-Host "[$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))] $Message"
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-Image([string]$Image) {
    docker image inspect $Image *> $null
    if ($LASTEXITCODE -ne 0) { throw "Docker image not found: $Image" }
}

if (-not $resolvedDeployDir.StartsWith($resolvedRepoRoot + [System.IO.Path]::DirectorySeparatorChar)) {
    throw "Deployment directory must stay inside the repository: $resolvedDeployDir"
}

Write-ProgressLog "[1/7] Preparing deployment directory"
New-Item -ItemType Directory -Force -Path $deployDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $deployDir "models") | Out-Null
foreach ($path in @($runtimeStage, (Join-Path $deployDir "source-mounted"), (Join-Path $deployDir "qasystem-runtime"))) {
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
Write-ProgressLog "[2/7] Copying Compose, environment template and deployment tools"
foreach ($item in @(
    @{ source = "docker-compose.yml"; destination = "docker-compose.yml" },
    @{ source = "download-models.ps1"; destination = "download-models.ps1" },
    @{ source = "run-offline-validation.ps1"; destination = "run-offline-validation.ps1" },
    @{ source = "docker/ENV_MIGRATION.md"; destination = "ENV_MIGRATION.md" },
    @{ source = "docker/RUNTIME_BASE.md"; destination = "RUNTIME_BASE.md" },
    @{ source = "docker/DEPLOY_PACKAGE_README.md"; destination = "README.md" },
    @{ source = "docker/apply-runtime.sh"; destination = "apply-runtime.sh" },
    @{ source = "docker/make-runtime-base.sh"; destination = "migrate-legacy-image-to-base.sh" }
)) {
    Copy-Item -LiteralPath (Join-Path $repoRoot $item.source) -Destination (Join-Path $deployDir $item.destination) -Force
}
$envTemplate = Get-Content -LiteralPath (Join-Path $repoRoot ".env.example") -Raw
$deliveryEnv = $envTemplate -replace "(?m)^HOST_MODELS_DIR=.*$", "HOST_MODELS_DIR=./models"
Set-Content -LiteralPath (Join-Path $deployDir ".env.example") -Value $deliveryEnv -Encoding utf8

Write-ProgressLog "[3/7] Staging the source-only runtime build context"
New-Item -ItemType Directory -Force -Path $runtimeStage | Out-Null
foreach ($directory in @("backend", "algorithm", "mcp_services", "agent")) {
    Copy-Item -LiteralPath (Join-Path $repoRoot $directory) -Destination (Join-Path $runtimeStage $directory) -Recurse -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $runtimeStage "docker") | Out-Null
foreach ($file in @("verify_runtime.py", "download_models.py", "offline_mock_llm.py", "offline_validation.py", "vllm_readiness.py")) {
    Copy-Item -LiteralPath (Join-Path $repoRoot "docker/$file") -Destination (Join-Path $runtimeStage "docker/$file") -Force
}
Copy-Item -LiteralPath (Join-Path $repoRoot "Dockerfile.runtime") -Destination (Join-Path $runtimeStage "Dockerfile.runtime") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot ".dockerignore") -Destination (Join-Path $runtimeStage ".dockerignore") -Force

# Remove runtime products only from the isolated staging tree.
foreach ($relative in @(
    "backend/data/documents", "backend/data/preproc_output", "backend/data/preproc_upload",
    "backend/chroma_db", "agent/image_result"
)) {
    $path = Join-Path $runtimeStage $relative
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
Get-ChildItem -LiteralPath $runtimeStage -Recurse -Directory -Force |
    Where-Object { $_.Name -in @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "node_modules") } |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $runtimeStage -Recurse -File -Force |
    Where-Object { $_.Extension -in @(".pyc", ".pyo", ".log") -or $_.Name -eq ".env" -or $_.Name -like ".env.*" } |
    Remove-Item -Force

Write-ProgressLog "[4/7] Creating runtime-context.tar (the dependency base is not embedded)"
if (Test-Path -LiteralPath $runtimeArchive) { Remove-Item -LiteralPath $runtimeArchive -Force }
Push-Location $runtimeStage
try {
    & tar.exe -cf $runtimeArchive .
    if ($LASTEXITCODE -ne 0) { throw "Failed to create runtime context archive" }
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $runtimeStage -Recurse -Force
}

$images = @()
Write-ProgressLog "[5/7] Processing optional one-time image archives"
if ($IncludeBaseImage) {
    Assert-Image $BaseImage
    $baseTar = Join-Path $deployDir "qasystem-base-v1.tar"
    if (-not $ReuseExistingArchives) {
        Write-ProgressLog "Exporting the one-time dependency base; this can take several minutes"
        docker save -o $baseTar $BaseImage
        if ($LASTEXITCODE -ne 0) { throw "Failed to export $BaseImage" }
    }
    if (-not (Test-Path -LiteralPath $baseTar) -or (Get-Item $baseTar).Length -eq 0) {
        throw "Missing reusable base archive: $baseTar"
    }
    $images += [ordered]@{ tag = $BaseImage; image_id = (docker image inspect $BaseImage --format "{{.Id}}").Trim(); archive = "qasystem-base-v1.tar"; sha256 = Get-Sha256 $baseTar }
}
if ($IncludeRedisImage) {
    docker image inspect $RedisImage *> $null
    if ($LASTEXITCODE -ne 0) { docker pull $RedisImage }
    if ($LASTEXITCODE -ne 0) { throw "Failed to obtain $RedisImage" }
    $redisTar = Join-Path $deployDir "redis-7-alpine.tar"
    if (-not $ReuseExistingArchives) {
        docker save -o $redisTar $RedisImage
        if ($LASTEXITCODE -ne 0) { throw "Failed to export $RedisImage" }
    }
    if (-not (Test-Path -LiteralPath $redisTar) -or (Get-Item $redisTar).Length -eq 0) {
        throw "Missing reusable Redis archive: $redisTar"
    }
    $images += [ordered]@{ tag = $RedisImage; image_id = (docker image inspect $RedisImage --format "{{.Id}}").Trim(); archive = "redis-7-alpine.tar"; sha256 = Get-Sha256 $redisTar }
}

Write-ProgressLog "[6/7] Writing manifest and SHA-256 checksums"
$deliveryFiles = @(
    "runtime-context.tar", "docker-compose.yml", ".env.example", "README.md", "ENV_MIGRATION.md", "RUNTIME_BASE.md",
    "apply-runtime.sh", "migrate-legacy-image-to-base.sh", "download-models.ps1", "run-offline-validation.ps1"
)
if ($IncludeBaseImage) { $deliveryFiles += "qasystem-base-v1.tar" }
if ($IncludeRedisImage) { $deliveryFiles += "redis-7-alpine.tar" }
$fileHashes = [ordered]@{}
$checksumLines = @()
foreach ($file in $deliveryFiles) {
    $hash = Get-Sha256 (Join-Path $deployDir $file)
    $fileHashes[$file] = $hash
    $checksumLines += "$hash  $file"
}
Set-Content -LiteralPath (Join-Path $deployDir "SHA256SUMS") -Value $checksumLines -Encoding ascii
$manifest = [ordered]@{
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    git_commit = (git -C $repoRoot rev-parse HEAD 2>$null)
    layout = "dependency-base-plus-runtime-overlay"
    base_image = $BaseImage
    runtime_image = $RuntimeImage
    runtime_delivery = "source-context-built-on-target"
    images = $images
    files = $fileHashes
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $deployDir "交付清单.json") -Encoding utf8

Write-ProgressLog "[7/7] Deployment bundle ready: $deployDir"
Write-ProgressLog "Normal releases transfer runtime-context.tar only. Use -IncludeBaseImage only for the first deployment or an incompatible base upgrade."
