. (Join-Path $PSScriptRoot "paths.ps1")
Assert-Environment
$reportDir = Join-Path $workspaceRoot ("tmp/test/runs/check-" + (Get-Date -Format "yyyyMMdd-HHmmss-fff"))
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
$previousPlatform = $env:QT_QPA_PLATFORM
$previousBytecode = $env:PYTHONDONTWRITEBYTECODE
$env:QT_QPA_PLATFORM = "offscreen"
$env:PYTHONDONTWRITEBYTECODE = "1"
Push-Location $sourceRoot
try {
    Invoke-Checked {
        & $python -m ruff check --config pyproject.toml --cache-dir ../tmp/test/cache/ruff src scripts ../tmp/test/tests ../tmp/test/benchmarks 2>&1 |
            Tee-Object -FilePath (Join-Path $reportDir "ruff.log")
    } "Ruff"
    Invoke-Checked {
        & $python -m mypy --cache-dir ../tmp/test/cache/mypy src 2>&1 |
            Tee-Object -FilePath (Join-Path $reportDir "mypy.log")
    } "mypy"
    # Each suite owns its QApplication lifetime. All cases still run; no retries or exclusions.
    foreach ($suite in @("unit", "ui", "integration")) {
        Invoke-Checked {
            & $python -m pytest -q "../tmp/test/tests/$suite" "--basetemp=$reportDir\$suite-tmp" 2>&1 |
                Tee-Object -FilePath (Join-Path $reportDir "pytest-$suite.log")
        } "pytest $suite"
    }
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
