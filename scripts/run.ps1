$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$env:UV_CACHE_DIR = Join-Path $repoRoot ".cache\uv"

Push-Location $repoRoot
try {
    & uv run python -m easypicture @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
