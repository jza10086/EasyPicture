param([switch]$AutoTest)
. (Join-Path $PSScriptRoot "paths.ps1")
Assert-Environment
$arguments = @("-m", "easypicture.ui.numeric_input_harness")
if ($AutoTest) { $arguments += "--auto-test" }
Push-Location $sourceRoot
try {
    & $python @arguments
    exit $LASTEXITCODE
}
finally { Pop-Location }
