param(
    [switch]$IncludeImage,
    [string]$ImageName = "qasystem-backend:latest",
    [switch]$SkipImageVerification
)

$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
$deployDir = Join-Path $repoRoot "deploy"
$runtimeDir = Join-Path $deployDir "qasystem-runtime"
$resolvedRepoRoot = [System.IO.Path]::GetFullPath($repoRoot)
$resolvedRuntimeDir = [System.IO.Path]::GetFullPath($runtimeDir)

if (-not $resolvedRuntimeDir.StartsWith($resolvedRepoRoot + [System.IO.Path]::DirectorySeparatorChar)) {
    throw "Runtime directory must stay inside the repository: $resolvedRuntimeDir"
}

New-Item -ItemType Directory -Force -Path $deployDir | Out-Null
if (Test-Path -LiteralPath $runtimeDir) {
    Remove-Item -LiteralPath $runtimeDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

foreach ($directory in @("backend", "algorithm", "agent", "mcp_services", "ofd_converter")) {
    $source = Join-Path $repoRoot $directory
    $destination = Join-Path $runtimeDir $directory
    robocopy $source $destination /E /NFL /NDL /NJH /NJS /NP `
        /XD __pycache__ .venv venv env .pytest_cache .mypy_cache .ruff_cache `
        /XF .env ".env.*" "*.pyc" "*.pyo" "*.log"
    if ($LASTEXITCODE -ge 8) {
        throw "Failed to copy $directory (robocopy exit code $LASTEXITCODE)"
    }
}

$manifest = [ordered]@{
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    git_commit = (git -C $repoRoot rev-parse HEAD 2>$null)
    image = $ImageName
    files = [ordered]@{}
}
foreach ($file in @("Dockerfile", "docker-compose.yml", "backend\requirements.txt", "agent\pyproject.toml")) {
    $path = Join-Path $repoRoot $file
    $manifest.files[$file] = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content `
    -LiteralPath (Join-Path $runtimeDir "部署清单.json") -Encoding utf8

$backendRuntimeData = Join-Path $runtimeDir "backend\data"
if (Test-Path -LiteralPath $backendRuntimeData) {
    Remove-Item -LiteralPath $backendRuntimeData -Recurse -Force
}

if ($IncludeImage) {
    docker image inspect $ImageName | Out-Null
    if (-not $SkipImageVerification) {
        docker run --rm --entrypoint python $ImageName /usr/local/bin/verify_qasystem_runtime.py
        if ($LASTEXITCODE -ne 0) {
            throw "Image verification failed (exit code $LASTEXITCODE): $ImageName"
        }
    }
    docker save -o (Join-Path $deployDir "qasystem-backend-latest.tar") $ImageName
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to export image (exit code $LASTEXITCODE): $ImageName"
    }
}

Write-Host "Runtime package generated at $runtimeDir"
Write-Host "The runtime directory is a source snapshot; Compose runs code baked into the verified image."
