param(
    [string]$ModelsDir = "",
    [ValidateSet("modelscope", "huggingface")]
    [string]$Source = "modelscope",
    [string]$ImageName = "qasystem-env-v1",
    [string]$SourceDir = $PSScriptRoot,
    [switch]$SkipDocling,
    [switch]$SkipMineruPipeline,
    [switch]$SkipMineruVlm,
    [switch]$SkipEmbedding,
    [switch]$Jev,
    [switch]$Clef
)

$ErrorActionPreference = "Stop"
if (-not $ModelsDir) {
    $ModelsDir = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot "runtime")) {
        Join-Path $PSScriptRoot "models"
    } else {
        Join-Path $PSScriptRoot "deploy\models"
    }
}
$resolvedModelsDir = [System.IO.Path]::GetFullPath($ModelsDir)
$resolvedSourceDir = [System.IO.Path]::GetFullPath($SourceDir)
if (-not (Test-Path -LiteralPath (Join-Path $resolvedSourceDir "docker\download_models.py"))) {
    $bundledSource = Join-Path $PSScriptRoot "runtime"
    if (Test-Path -LiteralPath (Join-Path $bundledSource "docker\download_models.py")) {
        $resolvedSourceDir = [System.IO.Path]::GetFullPath($bundledSource)
    }
    else {
        throw "Runtime source directory not found: $resolvedSourceDir"
    }
}
New-Item -ItemType Directory -Force -Path $resolvedModelsDir | Out-Null

docker image inspect $ImageName *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Image not found: $ImageName. Build or load the environment image before downloading models."
}

$arguments = @(
    "run", "--rm",
    "--mount", "type=bind,source=$resolvedModelsDir,target=/models",
    "--mount", "type=bind,source=$resolvedSourceDir\docker,target=/app/docker,readonly",
    "--entrypoint", "python",
    $ImageName,
    "/app/docker/download_models.py",
    "--source", $Source
)
if ($SkipDocling) { $arguments += "--skip-docling" }
if ($SkipMineruPipeline) { $arguments += "--skip-mineru-pipeline" }
if ($SkipMineruVlm) { $arguments += "--skip-mineru-vlm" }
if ($SkipEmbedding) { $arguments += "--skip-embedding" }
if ($Jev) { $arguments += "--jev" }
if ($Clef) { $arguments += "--clef" }

Write-Host "[$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))] Downloading external model files"
& docker @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Model download failed with exit code $LASTEXITCODE"
}

Write-Host "[$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss'))] Models prepared in $resolvedModelsDir"
