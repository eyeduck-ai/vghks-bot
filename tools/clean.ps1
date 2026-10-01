# Remove regenerable caches; optionally retain validation and the newest two ZIPs.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$KeepValidationReports,
    [switch]$OldReleases
)

$ErrorActionPreference = 'Stop'
$vghksRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$vghksPrefix = $vghksRoot + [IO.Path]::DirectorySeparatorChar

function Get-VghksCleanupPath([string]$Candidate) {
    $vghksTarget = (Resolve-Path -LiteralPath $Candidate).Path
    if (-not $vghksTarget.StartsWith($vghksPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Cleanup target outside workspace'
    }
    $vghksItem = Get-Item -LiteralPath $vghksTarget -Force
    if ($vghksItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Refusing to follow a linked cleanup target'
    }
    if ($vghksItem.PSIsContainer -and @(Get-ChildItem -LiteralPath $vghksTarget -Recurse -Force -Attributes ReparsePoint).Count) {
        throw 'Refusing to delete a directory containing links'
    }
    return $vghksTarget
}

$vghksCandidates = @('.ruff_cache', 'build') | ForEach-Object { Join-Path $vghksRoot $_ }
$vghksBuild = Join-Path $vghksRoot '.build'
if (Test-Path -LiteralPath $vghksBuild) {
    $vghksBuild = Get-VghksCleanupPath $vghksBuild
    if ($KeepValidationReports) {
        $vghksCandidates += @(Get-ChildItem -LiteralPath $vghksBuild -Force |
            Where-Object { $_.Name -ne 'ci' } | Select-Object -ExpandProperty FullName)
    } else {
        $vghksCandidates += $vghksBuild
    }
}
foreach ($vghksSource in @('vghks_bot', 'tests', 'tools')) {
    $vghksSourcePath = Join-Path $vghksRoot $vghksSource
    if (Test-Path -LiteralPath $vghksSourcePath) {
        $vghksSourcePath = Get-VghksCleanupPath $vghksSourcePath
        $vghksCandidates += @(Get-ChildItem -LiteralPath $vghksSourcePath -Directory -Recurse -Force -Filter '__pycache__' |
            Select-Object -ExpandProperty FullName)
    }
}
if ($OldReleases) {
    $vghksDist = Join-Path $vghksRoot 'dist'
    if (Test-Path -LiteralPath $vghksDist) {
        $vghksDist = Get-VghksCleanupPath $vghksDist
        $vghksArchives = @(Get-ChildItem -LiteralPath $vghksDist -File -Filter 'VGHKS-bot-v*-windows-x64.zip' |
            ForEach-Object {
                if ($_.Name -match '^VGHKS-bot-v(?<version>\d+\.\d+\.\d+)-windows-x64\.zip$') {
                    [pscustomobject]@{Path=$_.FullName; Version=[version]$Matches.version}
                }
            } | Sort-Object Version -Descending)
        $vghksCandidates += @($vghksArchives | Select-Object -Skip 2 -ExpandProperty Path)
    }
}

# Validate every candidate before deleting the first one.
$vghksTargets = @($vghksCandidates | Select-Object -Unique | ForEach-Object {
    if (Test-Path -LiteralPath $_) { Get-VghksCleanupPath $_ }
})
$vghksRemoved = @()
$vghksBytes = 0L
foreach ($vghksTarget in $vghksTargets) {
    if ($PSCmdlet.ShouldProcess($vghksTarget, 'Remove regenerable cache or old release')) {
        $vghksItem = Get-Item -LiteralPath $vghksTarget -Force
        if ($vghksItem.PSIsContainer) {
            $vghksBytes += (Get-ChildItem -LiteralPath $vghksTarget -File -Recurse -Force | Measure-Object Length -Sum).Sum
        } else {
            $vghksBytes += $vghksItem.Length
        }
        Remove-Item -LiteralPath $vghksTarget -Recurse -Force
        $vghksRemoved += $vghksTarget.Substring($vghksPrefix.Length)
    }
}
[pscustomobject]@{Removed=$vghksRemoved;BytesFreed=$vghksBytes} | ConvertTo-Json -Depth 3
