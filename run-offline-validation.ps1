param(
    [string]$ImageName = "qasystem-backend:latest",
    [string]$ModelsDir = (Join-Path $PSScriptRoot "deploy\models"),
    [string]$SamplesDir = (Join-Path $PSScriptRoot "test_artifacts\parser")
)

$ErrorActionPreference = "Stop"
$modelsPath = [System.IO.Path]::GetFullPath($ModelsDir)
$samplesPath = [System.IO.Path]::GetFullPath($SamplesDir)
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

$env:BACKEND_IMAGE = $ImageName
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
