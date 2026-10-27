. (Join-Path $PSScriptRoot "paths.ps1")
Assert-Environment
Push-Location $sourceRoot
try { Invoke-Checked { & $python (Join-Path $PSScriptRoot "build_release.py") } "Release packaging" }
finally { Pop-Location }
