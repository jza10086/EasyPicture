. (Join-Path $PSScriptRoot "paths.ps1")
Assert-Environment
$executable = Join-Path $workspaceRoot "release\EasyPicture.exe"
if (-not (Test-Path -LiteralPath $executable)) { throw "Run source\scripts\package.ps1 first." }
$reportDir = New-RunDirectory "release-check"
Invoke-Checked {
    & $python (Join-Path $PSScriptRoot "release_smoke.py") --executable $executable --output (Join-Path $reportDir "smoke")
} "Release smoke tests"
Write-Output "Release check reports: $reportDir"
