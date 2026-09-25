# Fily installer for Windows.
#
#   install.cmd             first time: install, then run setup
#                           afterwards: update dependencies and re-arm the schedule
#   install.cmd -Setup      run setup again (change keys, folders, time)
#
# Plain ASCII on purpose: Windows PowerShell 5.1 misreads UTF-8 scripts that
# have no byte-order mark.
param([switch]$Setup)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$MinMinor = 11

function Fail($msg) { Write-Host $msg -ForegroundColor Red; exit 1 }

if ($env:OS -ne 'Windows_NT') { Fail 'install.ps1 is for Windows. On a Mac, run ./install.sh' }

Write-Host 'Installing Fily...' -ForegroundColor Cyan

$venvPy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$uv = Get-Command uv -ErrorAction SilentlyContinue

if ($uv) {
    # uv fetches a suitable Python by itself if this PC doesn't have one.
    & uv venv --quiet --allow-existing --python ">=3.$MinMinor" .venv
    if ($LASTEXITCODE -ne 0) { Fail 'Could not create the Python environment.' }
    & uv pip install --quiet --python $venvPy -e .
    if ($LASTEXITCODE -ne 0) { Fail 'Could not install dependencies.' }
} else {
    # Prefer the py launcher; skip the Microsoft Store "python" stub, which
    # opens the Store instead of running anything.
    $pyExe = $null; $pyArgs = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in '3.13', '3.12', '3.14', '3.11') {
            & py "-$v" -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) { $pyExe = 'py'; $pyArgs = @("-$v"); break }
        }
    }
    if (-not $pyExe) {
        $cand = Get-Command python -ErrorAction SilentlyContinue
        if ($cand -and $cand.Source -notlike '*WindowsApps*') {
            & $cand.Source -c "import sys; sys.exit(sys.version_info < (3, $MinMinor))" 2>$null
            if ($LASTEXITCODE -eq 0) { $pyExe = $cand.Source }
        }
    }
    if (-not $pyExe) {
        Write-Host ''
        Write-Host "Fily needs Python 3.$MinMinor or newer, and this PC doesn't have it." -ForegroundColor Yellow
        Write-Host 'Install it one of these ways, then run install.cmd again:'
        Write-Host '  - winget install Python.Python.3.13'
        Write-Host '  - or download it from https://www.python.org/downloads/windows/'
        Write-Host '    (tick "Add python.exe to PATH" in the installer)'
        exit 1
    }
    & $pyExe @pyArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail 'Could not create the Python environment.' }
    & $venvPy -m pip install --quiet --upgrade pip
    & $venvPy -m pip install --quiet -e .
    if ($LASTEXITCODE -ne 0) { Fail 'Could not install dependencies.' }
}

Write-Host "OK - installed into $PSScriptRoot\.venv" -ForegroundColor Green

if ($Setup -or -not (Test-Path -LiteralPath 'config.yaml')) {
    & $venvPy -X utf8 -m organizer.cli setup
    exit $LASTEXITCODE
}

# Already set up: this was an update. Re-register the tasks so they point at
# this copy's code, and restart the bot onto it.
& $venvPy -X utf8 -m organizer.cli install
Write-Host ''
Write-Host 'Updated. Nothing else to do.' -ForegroundColor Green
Write-Host 'Change keys, folders or time with:  install.cmd -Setup'
exit $LASTEXITCODE
