. (Join-Path $PSScriptRoot "paths.ps1")
Assert-Environment
Push-Location $sourceRoot
try {
    & $python -m easypicture @args
    exit $LASTEXITCODE
}
finally { Pop-Location }
