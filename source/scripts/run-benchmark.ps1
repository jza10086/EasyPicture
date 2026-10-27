param([ValidateSet("p0", "p2", "p3", "p4", "p5", "objects", "interaction")][string]$Scene = "objects")
. (Join-Path $PSScriptRoot "paths.ps1")
Assert-Environment
$reportDir = New-RunDirectory "benchmark-$Scene"
$previousPlatform = $env:QT_QPA_PLATFORM
$previousOutput = $env:EASYPICTURE_BENCHMARK_DIR
$env:QT_QPA_PLATFORM = "offscreen"
$env:EASYPICTURE_BENCHMARK_DIR = $reportDir
$filename = switch ($Scene) {
    "objects" { "object_refactor_benchmark.py" }
    "interaction" { "object_interaction_benchmark.py" }
    default { "$($Scene)_benchmark.py" }
}
$entry = Join-Path $archiveRoot "validation\benchmarks\$filename"
Push-Location $sourceRoot
try {
    if ($Scene -in @("objects", "interaction")) {
        Invoke-Checked { & $python $entry --output (Join-Path $reportDir "result.json") } "Benchmark"
    } else { Invoke-Checked { & $python $entry } "Benchmark" }
    Write-Output "Benchmark reports: $reportDir"
}
finally {
    Pop-Location
    $env:QT_QPA_PLATFORM = $previousPlatform
    $env:EASYPICTURE_BENCHMARK_DIR = $previousOutput
}
