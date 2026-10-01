[CmdletBinding()]
param([switch]$RebuildEnvironment)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$vghksProjectPath = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\', '/')
$vghksEnvironmentPath = Join-Path $vghksProjectPath '.venv'
$vghksPythonPath = Join-Path $vghksEnvironmentPath 'Scripts\python.exe'
$vghksActivationPath = Join-Path $vghksEnvironmentPath 'Scripts\activate.bat'
$vghksNeedsEnvironment = $RebuildEnvironment -or
    -not (Test-Path -LiteralPath $vghksPythonPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $vghksActivationPath -PathType Leaf)

if (-not $vghksNeedsEnvironment) {
    $vghksActivation = Get-Content -LiteralPath $vghksActivationPath -Raw
    $vghksActivationMatch = [regex]::Match($vghksActivation, '(?im)^set\s+"?VIRTUAL_ENV=(.+)$')
    $vghksRecordedPath = $vghksActivationMatch.Groups[1].Value.Trim().Trim('"')
    $vghksNeedsEnvironment = -not [string]::Equals(
        $vghksRecordedPath, $vghksEnvironmentPath, [StringComparison]::OrdinalIgnoreCase
    )
}

if ($vghksNeedsEnvironment) {
    if ([IO.Path]::GetDirectoryName($vghksEnvironmentPath) -ne $vghksProjectPath) {
        throw 'Python environment must stay within the project directory.'
    }
    if (Test-Path -LiteralPath $vghksEnvironmentPath) {
        $vghksEnvironmentItem = Get-Item -LiteralPath $vghksEnvironmentPath -Force
        if (-not $vghksEnvironmentItem.PSIsContainer -or
            ($vghksEnvironmentItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Cannot rebuild a Python environment that is a file or directory link.'
        }
        $vghksEnvironmentLinks = Get-ChildItem -LiteralPath $vghksEnvironmentPath -Recurse -Force |
            Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
            Select-Object -First 1
        if ($vghksEnvironmentLinks) {
            throw 'Cannot rebuild a Python environment containing directory or file links.'
        }
        Write-Host 'Rebuilding Python environment for the current project path.'
        py -3.11 -m venv --clear $vghksEnvironmentPath
    } else {
        py -3.11 -m venv $vghksEnvironmentPath
    }
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create Python environment.' }
}
& $vghksPythonPath -m pip install -r requirements-build.lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $vghksPythonPath -X utf8 tools\run_checks.py --build
if ($LASTEXITCODE -ne 0) { throw 'Validation or EXE build failed.' }
