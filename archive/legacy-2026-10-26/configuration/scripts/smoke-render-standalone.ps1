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
    foreach ($backend in @("software", "opengl")) {
        $smokeDir = Join-Path $repoRoot ".cache\standalone-render-$backend"
        New-Item -ItemType Directory -Path $smokeDir -Force | Out-Null
        $process = Start-Process `
            -FilePath $executable.FullName `
            -ArgumentList @(
                "--render-backend",
                $backend,
                "--render-smoke-test",
                $smokeDir
            ) `
            -Wait `
            -PassThru
        if ($process.ExitCode -ne 0) {
            $diagnostics = Join-Path $smokeDir "diagnostics.json"
            $detail = if (Test-Path -LiteralPath $diagnostics) {
                Get-Content -LiteralPath $diagnostics -Raw
            }
            else {
                "diagnostics.json was not created"
            }
            throw "Standalone $backend render smoke failed: $detail"
        }
        Write-Output "Standalone $backend render smoke passed: $smokeDir"
    }
}
finally {
    $env:PATH = $originalPath
}
