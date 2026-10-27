$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$env:UV_CACHE_DIR = Join-Path $repoRoot ".cache\uv"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

Push-Location $repoRoot
try {
    Invoke-Checked { uv lock --check } "uv lock check"
    Invoke-Checked { uv run ruff check . } "Ruff"
    Invoke-Checked { uv run mypy src } "mypy"
    Invoke-Checked { uv run pytest -q } "pytest"
    Invoke-Checked { uv run python -m easypicture --smoke-test } "application smoke test"
    $ioSmokeDir = Join-Path $repoRoot ".cache\io-smoke"
    New-Item -ItemType Directory -Path $ioSmokeDir -Force | Out-Null
    Invoke-Checked {
        uv run python -m easypicture --io-smoke-test $ioSmokeDir
    } "persistence I/O smoke test"
}
finally {
    Pop-Location
}
