param(
    [string]$ImageName = "qasystem-env-v1",
    [string]$SourceDir = $PSScriptRoot,
    [string]$ModelsDir = "",
    [string]$SamplesDir = (Join-Path $PSScriptRoot "test_data\parse")
)

$ErrorActionPreference = "Stop"
if (-not $ModelsDir) {
    $ModelsDir = if (Test-Path -LiteralPath (Join-Path $PSScriptRoot "runtime")) {
        Join-Path $PSScriptRoot "models"
    } else {
        Join-Path $PSScriptRoot "deploy\models"
    }
}
$modelsPath = [System.IO.Path]::GetFullPath($ModelsDir)
$samplesPath = [System.IO.Path]::GetFullPath($SamplesDir)
$sourcePath = [System.IO.Path]::GetFullPath($SourceDir)
if (-not (Test-Path -LiteralPath (Join-Path $sourcePath "backend"))) {
    $bundledSource = Join-Path $PSScriptRoot "runtime"
    if (Test-Path -LiteralPath (Join-Path $bundledSource "backend")) {
        $sourcePath = [System.IO.Path]::GetFullPath($bundledSource)
    }
    else {
        throw "Runtime source directory not found: $sourcePath"
    }
}
if (-not (Test-Path -LiteralPath $modelsPath)) {
    throw "Models directory not found: $modelsPath"
}
if (-not (Test-Path -LiteralPath $samplesPath)) {
    $bundledSamples = Join-Path $PSScriptRoot "offline-validation\parser"
    if (Test-Path -LiteralPath $bundledSamples) {
        $samplesPath = [System.IO.Path]::GetFullPath($bundledSamples)
    }
    else {
        throw "Parser samples directory not found: $samplesPath"
    }
}

$composeFile = Join-Path $PSScriptRoot "docker\docker-compose.offline-test.yml"
if (-not (Test-Path -LiteralPath $composeFile)) {
    $composeFile = Join-Path $PSScriptRoot "offline-validation\docker-compose.offline-test.yml"
}

docker image inspect $ImageName *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Image not found: $ImageName"
}

$env:QASYSTEM_ENV_IMAGE = $ImageName
$env:QASYSTEM_SOURCE_DIR = $sourcePath
$env:HOST_MODELS_DIR = $modelsPath
$env:OFFLINE_SAMPLES_DIR = $samplesPath
try {
    docker compose -p qasystem-offline-test -f $composeFile up --abort-on-container-exit --exit-code-from verifier
    if ($LASTEXITCODE -ne 0) {
        throw "Offline validation failed with exit code $LASTEXITCODE"
    }
}
finally {
    docker compose -p qasystem-offline-test -f $composeFile down --remove-orphans
}
