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
    $ioSmokeDir = Join-Path $repoRoot ".cache\standalone-io-smoke"
    New-Item -ItemType Directory -Path $ioSmokeDir -Force | Out-Null
    $ioProcess = Start-Process `
        -FilePath $executable.FullName `
        -ArgumentList @("--io-smoke-test", $ioSmokeDir) `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($ioProcess.ExitCode -ne 0) {
        throw "Standalone I/O smoke test failed with exit code $($ioProcess.ExitCode)"
    }
    $svgSmokeDir = Join-Path $repoRoot ".cache\standalone-svg-smoke"
    New-Item -ItemType Directory -Path $svgSmokeDir -Force | Out-Null
    $svgProcess = Start-Process `
        -FilePath $executable.FullName `
        -ArgumentList @("--svg-smoke-test", $svgSmokeDir) `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($svgProcess.ExitCode -ne 0) {
        throw "Standalone SVG smoke test failed with exit code $($svgProcess.ExitCode)"
    }
    Write-Output "Standalone smoke test passed: $($executable.FullName)"
    Write-Output "Standalone I/O smoke test passed: $ioSmokeDir"
    Write-Output "Standalone SVG smoke test passed: $svgSmokeDir"
}
finally {
    $env:PATH = $originalPath
}
