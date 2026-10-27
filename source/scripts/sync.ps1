. (Join-Path $PSScriptRoot "paths.ps1")
Push-Location $sourceRoot
try { Invoke-Checked { uv sync --locked --all-groups } "Dependency synchronization" }
finally { Pop-Location }
