$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$env:UV_CACHE_DIR = Join-Path $repoRoot ".cache\uv"
$env:NUITKA_CACHE_DIR = Join-Path $repoRoot ".cache\nuitka"
$outputDir = Join-Path $repoRoot "build\nuitka"
$entryPoint = Join-Path $repoRoot "src\easypicture"

Push-Location $repoRoot
try {
    & uv run python -m nuitka `
        --mode=standalone `
        --python-flag=-m `
        --enable-plugin=pyside6 `
        --msvc=latest `
        --assume-yes-for-downloads `
        --windows-console-mode=disable `
        --output-filename=EasyPicture.exe `
        "--output-dir=$outputDir" `
        --remove-output `
        $entryPoint
    if ($LASTEXITCODE -ne 0) {
        throw "Nuitka build failed with exit code $LASTEXITCODE"
    }

    $executable = Get-ChildItem -LiteralPath $outputDir -Filter "EasyPicture.exe" -Recurse |
        Select-Object -First 1
    if (-not $executable) {
        throw "Nuitka completed but EasyPicture.exe was not found under $outputDir"
    }
    Write-Output "Standalone executable: $($executable.FullName)"
}
finally {
    Pop-Location
}
