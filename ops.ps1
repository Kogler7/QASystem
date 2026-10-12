param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ControlArguments)
$controller = Join-Path $PSScriptRoot 'ops.py'
$localPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (Test-Path -LiteralPath $localPython) { & $localPython $controller @ControlArguments }
else { & python $controller @ControlArguments }
exit $LASTEXITCODE
