param(
    [switch]$IncludeEnvImage,
    [switch]$IncludeRedisImage,
    [string]$EnvImage = "qasystem-env-v1",
    [string]$RedisImage = "redis:7-alpine",
    [switch]$ReuseExistingArchives
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$deployDir = Join-Path $repoRoot "deploy"
$runtimeDir = Join-Path $deployDir "runtime"
$offlineDir = Join-Path $deployDir "offline-validation"
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

Write-ProgressLog "[1/6] Preparing deployment directory"
New-Item -ItemType Directory -Force -Path $deployDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $deployDir "models") | Out-Null
New-Item -ItemType Directory -Force -Path $offlineDir | Out-Null
if (-not $IncludeEnvImage -and (Test-Path -LiteralPath (Join-Path $deployDir "qasystem-env-v1.tar"))) {
    Remove-Item -LiteralPath (Join-Path $deployDir "qasystem-env-v1.tar") -Force
}
if (-not $IncludeRedisImage -and (Test-Path -LiteralPath (Join-Path $deployDir "redis-7-alpine.tar"))) {
    Remove-Item -LiteralPath (Join-Path $deployDir "redis-7-alpine.tar") -Force
}
if (Test-Path -LiteralPath $runtimeDir) { Remove-Item -LiteralPath $runtimeDir -Recurse -Force }

Write-ProgressLog "[2/6] Copying Compose, environment template and deployment documentation"
foreach ($item in @(
    @{ source = "docker-compose.yml"; destination = "docker-compose.yml" },
    @{ source = "download-models.ps1"; destination = "download-models.ps1" },
    @{ source = "run-offline-validation.ps1"; destination = "run-offline-validation.ps1" },
    @{ source = "docker/ENV_MIGRATION.md"; destination = "ENV_MIGRATION.md" },
    @{ source = "docker/DEPLOY_PACKAGE_README.md"; destination = "README.md" }
)) {
    Copy-Item -LiteralPath (Join-Path $repoRoot $item.source) -Destination (Join-Path $deployDir $item.destination) -Force
}
Copy-Item -LiteralPath (Join-Path $repoRoot "docker/docker-compose.offline-test.yml") -Destination (Join-Path $offlineDir "docker-compose.offline-test.yml") -Force
Copy-Item -LiteralPath (Join-Path $repoRoot "docker/OFFLINE_VALIDATION.md") -Destination (Join-Path $offlineDir "README.md") -Force
if (Test-Path -LiteralPath (Join-Path $offlineDir "parser")) { Remove-Item -LiteralPath (Join-Path $offlineDir "parser") -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $repoRoot "test_artifacts/parser") -Destination (Join-Path $offlineDir "parser") -Recurse -Force
$envTemplate = Get-Content -LiteralPath (Join-Path $repoRoot ".env.example") -Raw
$deliveryEnv = $envTemplate `
    -replace "(?m)^HOST_MODELS_DIR=.*$", "HOST_MODELS_DIR=./models" `
    -replace "(?m)^QASYSTEM_SOURCE_DIR=.*$", "QASYSTEM_SOURCE_DIR=./runtime"
Set-Content -LiteralPath (Join-Path $deployDir ".env.example") -Value $deliveryEnv -Encoding utf8

Write-ProgressLog "[3/6] Creating the read-only runtime source directory"
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
foreach ($directory in @("backend", "algorithm", "mcp_services", "agent")) {
    Copy-Item -LiteralPath (Join-Path $repoRoot $directory) -Destination (Join-Path $runtimeDir $directory) -Recurse -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $runtimeDir "docker") | Out-Null
foreach ($file in @("verify_runtime.py", "download_models.py", "offline_mock_llm.py", "offline_validation.py", "vllm_readiness.py")) {
    Copy-Item -LiteralPath (Join-Path $repoRoot "docker/$file") -Destination (Join-Path $runtimeDir "docker/$file") -Force
}
foreach ($relative in @(
    "backend/data/documents", "backend/data/preproc_output", "backend/data/preproc_upload",
    "backend/chroma_db", "agent/image_result"
)) {
    $path = Join-Path $runtimeDir $relative
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
Get-ChildItem -LiteralPath $runtimeDir -Recurse -Directory -Force |
    Where-Object {
        $_.Name -in @("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "node_modules", "build", "tests", "docs") `
            -or $_.Name -like "*.egg-info"
    } |
    Sort-Object FullName -Descending |
    Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $runtimeDir -Recurse -File -Force |
    Where-Object { $_.Extension -in @(".pyc", ".pyo", ".log") -or $_.Name -eq ".env" -or $_.Name -like ".env.*" } |
    Remove-Item -Force
Set-Content -LiteralPath (Join-Path $runtimeDir "SOURCE_COMMIT") -Value (git -C $repoRoot rev-parse HEAD 2>$null) -Encoding ascii

$images = @()
Write-ProgressLog "[4/6] Processing optional environment and Redis image archives"
if ($IncludeEnvImage) {
    Assert-Image $EnvImage
    $envTar = Join-Path $deployDir "qasystem-env-v1.tar"
    if (-not $ReuseExistingArchives) {
        Write-ProgressLog "Exporting source-free environment image; this can take several minutes"
        docker save -o $envTar $EnvImage
        if ($LASTEXITCODE -ne 0) { throw "Failed to export $EnvImage" }
    }
    if (-not (Test-Path -LiteralPath $envTar) -or (Get-Item $envTar).Length -eq 0) { throw "Missing environment image archive: $envTar" }
    $images += [ordered]@{ tag = $EnvImage; image_id = (docker image inspect $EnvImage --format "{{.Id}}").Trim(); archive = "qasystem-env-v1.tar"; sha256 = Get-Sha256 $envTar }
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
    if (-not (Test-Path -LiteralPath $redisTar) -or (Get-Item $redisTar).Length -eq 0) { throw "Missing Redis archive: $redisTar" }
    $images += [ordered]@{ tag = $RedisImage; image_id = (docker image inspect $RedisImage --format "{{.Id}}").Trim(); archive = "redis-7-alpine.tar"; sha256 = Get-Sha256 $redisTar }
}

Write-ProgressLog "[5/6] Writing manifest and SHA-256 checksums"
$excludedNames = @("SHA256SUMS", "交付清单.json")
$deliveryFiles = Get-ChildItem -LiteralPath $deployDir -Recurse -File -Force |
    Where-Object { $_.Name -notin $excludedNames -and -not $_.FullName.StartsWith((Join-Path $deployDir "models")) } |
    Sort-Object FullName
$fileHashes = [ordered]@{}
$checksumLines = @()
foreach ($file in $deliveryFiles) {
    $relative = [System.IO.Path]::GetRelativePath($deployDir, $file.FullName).Replace('\', '/')
    $hash = Get-Sha256 $file.FullName
    $fileHashes[$relative] = $hash
    $checksumLines += "$hash  $relative"
}
Set-Content -LiteralPath (Join-Path $deployDir "SHA256SUMS") -Value $checksumLines -Encoding ascii
$manifest = [ordered]@{
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    git_commit = (git -C $repoRoot rev-parse HEAD 2>$null)
    layout = "source-free-environment-image-with-read-only-source-mount"
    environment_image = $EnvImage
    source_directory = "runtime"
    images = $images
    files = $fileHashes
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $deployDir "交付清单.json") -Encoding utf8

Write-ProgressLog "[6/6] Deployment bundle ready: $deployDir"
Write-ProgressLog "Ordinary releases replace deploy/runtime and recreate Backend/Agent; no image rebuild is required."
