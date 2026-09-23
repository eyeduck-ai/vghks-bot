$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    py -3.11 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create Python environment.' }
}
& .venv\Scripts\python.exe -m pip install -r requirements-build.lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& .venv\Scripts\python.exe -X utf8 tools\run_checks.py --build
if ($LASTEXITCODE -ne 0) { throw 'Validation or EXE build failed.' }
