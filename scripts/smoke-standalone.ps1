$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$outputDir = Join-Path $repoRoot "build\nuitka"
$executable = Get-ChildItem -LiteralPath $outputDir -Filter "EasyPicture.exe" -Recurse |
    Select-Object -First 1

if (-not $executable) {
    throw "EasyPicture.exe was not found. Run scripts\package.ps1 first."
}

$originalPath = $env:PATH
try {
    $env:PATH = "$env:WINDIR\System32;$env:WINDIR"
    $process = Start-Process `
        -FilePath $executable.FullName `
        -ArgumentList "--smoke-test" `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Standalone smoke test failed with exit code $($process.ExitCode)"
    }
    Write-Output "Standalone smoke test passed: $($executable.FullName)"
}
finally {
    $env:PATH = $originalPath
}
