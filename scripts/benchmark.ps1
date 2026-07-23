$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$env:UV_CACHE_DIR = Join-Path $repoRoot ".cache\uv"

Push-Location $repoRoot
try {
    & uv run python benchmarks/p0_benchmark.py
    if ($LASTEXITCODE -ne 0) {
        throw "P0 benchmark failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
