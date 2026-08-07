param(
    [string]$ModelsDir = (Join-Path $PSScriptRoot "deploy\models"),
    [ValidateSet("modelscope", "huggingface")]
    [string]$Source = "modelscope",
    [string]$ImageName = "qasystem-runtime-v1",
    [switch]$SkipDocling,
    [switch]$SkipMineruPipeline,
    [switch]$SkipMineruVlm,
    [switch]$SkipEmbedding
)

$ErrorActionPreference = "Stop"
$resolvedModelsDir = [System.IO.Path]::GetFullPath($ModelsDir)
New-Item -ItemType Directory -Force -Path $resolvedModelsDir | Out-Null

docker image inspect $ImageName *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Image not found: $ImageName. Build or load the runtime image before downloading models."
}

$arguments = @(
    "run", "--rm",
    "--mount", "type=bind,source=$resolvedModelsDir,target=/models",
    "--entrypoint", "python",
    $ImageName,
    "/usr/local/bin/download_qasystem_models.py",
    "--source", $Source
)
if ($SkipDocling) { $arguments += "--skip-docling" }
if ($SkipMineruPipeline) { $arguments += "--skip-mineru-pipeline" }
if ($SkipMineruVlm) { $arguments += "--skip-mineru-vlm" }
if ($SkipEmbedding) { $arguments += "--skip-embedding" }

& docker @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Model download failed with exit code $LASTEXITCODE"
}

Write-Host "Models prepared in $resolvedModelsDir"
