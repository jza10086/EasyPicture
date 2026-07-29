param(
    [switch]$AutoTest
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:UV_CACHE_DIR = Join-Path $projectRoot ".cache\uv"

$arguments = @(
    "run",
    "python",
    "-m",
    "easypicture.ui.numeric_input_harness"
)
if ($AutoTest) {
    $arguments += "--auto-test"
}

Push-Location $projectRoot
try {
    & uv @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
