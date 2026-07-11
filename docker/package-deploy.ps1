param(
    [switch]$IncludeImage,
    [string]$ImageName = "qasystem-backend:latest"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
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

foreach ($directory in @("backend", "algorithm", "agentcode", "mcp_services")) {
    $source = Join-Path $repoRoot $directory
    $destination = Join-Path $runtimeDir $directory
    robocopy $source $destination /E /NFL /NDL /NJH /NJS /NP `
        /XD __pycache__ .venv venv env .pytest_cache .mypy_cache .ruff_cache `
        /XF .env ".env.*" "*.pyc" "*.pyo" "*.log"
    if ($LASTEXITCODE -ge 8) {
        throw "Failed to copy $directory (robocopy exit code $LASTEXITCODE)"
    }
}

$backendRuntimeData = Join-Path $runtimeDir "backend\data"
if (Test-Path -LiteralPath $backendRuntimeData) {
    Remove-Item -LiteralPath $backendRuntimeData -Recurse -Force
}

Copy-Item -LiteralPath (Join-Path $PSScriptRoot "docker-compose.deploy.yml") -Destination (Join-Path $deployDir "docker-compose.yml") -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot ".env.deploy.example") -Destination (Join-Path $deployDir ".env.example") -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "DEPLOY_README.md") -Destination (Join-Path $deployDir "README.md") -Force

$deployEnv = Join-Path $deployDir ".env"
if (-not (Test-Path -LiteralPath $deployEnv)) {
    Copy-Item -LiteralPath (Join-Path $deployDir ".env.example") -Destination $deployEnv
    Write-Warning "Created deploy/.env from the template; review it before starting the service."
}

if ($IncludeImage) {
    docker image inspect $ImageName | Out-Null
    docker save -o (Join-Path $deployDir "qasystem-backend-latest.tar") $ImageName
}

Write-Host "Deployment files generated at $deployDir"
