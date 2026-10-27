$ErrorActionPreference = "Stop"
$sourceRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $sourceRoot
$archiveRoot = Join-Path $workspaceRoot "archive"
$env:UV_CACHE_DIR = Join-Path $sourceRoot ".cache\uv"
$env:NUITKA_CACHE_DIR = Join-Path $sourceRoot ".cache\nuitka"
$python = Join-Path $sourceRoot ".venv\Scripts\python.exe"

function Invoke-Checked {
    param([scriptblock]$Command, [string]$Description)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

function New-RunDirectory {
    param([string]$Kind)
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
    $directory = Join-Path $archiveRoot "runs\$Kind-$stamp"
    New-Item -ItemType Directory -Path $directory -ErrorAction Stop | Out-Null
    return $directory
}

function Assert-Environment {
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Python environment missing. Run source\scripts\sync.ps1 first."
    }
}
