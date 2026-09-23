# Remove only regenerable build/cache directories. Data, .local, .venv and dist remain.
$ErrorActionPreference = 'Stop'
$vghksRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$vghksCandidates = @('.build', '.ruff_cache', 'build') | ForEach-Object { Join-Path $vghksRoot $_ }
foreach ($vghksSource in @('opd_monitor', 'tests', 'tools')) {
    $vghksCandidates += @(Get-ChildItem -LiteralPath (Join-Path $vghksRoot $vghksSource) -Directory -Recurse -Force -Filter '__pycache__' | Select-Object -ExpandProperty FullName)
}
$vghksRemoved = @()
$vghksBytes = 0L
foreach ($vghksCandidate in $vghksCandidates) {
    if (-not (Test-Path -LiteralPath $vghksCandidate)) { continue }
    $vghksTarget = (Resolve-Path -LiteralPath $vghksCandidate).Path
    if (-not $vghksTarget.StartsWith($vghksRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Cleanup target outside workspace' }
    if ((Get-Item -LiteralPath $vghksTarget -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Refusing to follow a linked cleanup directory' }
    if (@(Get-ChildItem -LiteralPath $vghksTarget -Recurse -Force -Attributes ReparsePoint).Count) { throw 'Refusing to delete a directory containing links' }
    $vghksBytes += (Get-ChildItem -LiteralPath $vghksTarget -File -Recurse -Force | Measure-Object Length -Sum).Sum
    Remove-Item -LiteralPath $vghksTarget -Recurse -Force
    $vghksRemoved += $vghksTarget.Substring($vghksRoot.Length + 1)
}
[pscustomobject]@{Removed=$vghksRemoved;BytesFreed=$vghksBytes}
