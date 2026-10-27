. (Join-Path $PSScriptRoot "paths.ps1")
Assert-Environment
$reportDir = New-RunDirectory "check"
$previousPlatform = $env:QT_QPA_PLATFORM
$previousBytecode = $env:PYTHONDONTWRITEBYTECODE
$env:QT_QPA_PLATFORM = "offscreen"
$env:PYTHONDONTWRITEBYTECODE = "1"
Push-Location $sourceRoot
try {
    Invoke-Checked {
        & $python -m ruff check --config pyproject.toml src scripts ../archive/validation/tests ../archive/validation/benchmarks 2>&1 |
            Tee-Object -FilePath (Join-Path $reportDir "ruff.log")
    } "Ruff"
    Invoke-Checked {
        & $python -m mypy --cache-dir .cache/mypy src 2>&1 |
            Tee-Object -FilePath (Join-Path $reportDir "mypy.log")
    } "mypy"
    Invoke-Checked {
        & $python -m pytest -q "--junitxml=$reportDir\tests.xml" "--basetemp=$reportDir\tmp" 2>&1 |
            Tee-Object -FilePath (Join-Path $reportDir "pytest.log")
    } "pytest"
    Invoke-Checked {
        & $python (Join-Path $PSScriptRoot "release_smoke.py") --source --output (Join-Path $reportDir "smoke")
    } "Source smoke tests"
    Write-Output "Check reports: $reportDir"
}
finally {
    Pop-Location
    $env:QT_QPA_PLATFORM = $previousPlatform
    $env:PYTHONDONTWRITEBYTECODE = $previousBytecode
}
