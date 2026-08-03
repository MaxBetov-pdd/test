[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root '.venv'
$Python = Get-Command python -ErrorAction SilentlyContinue

if (-not $Python) {
    throw 'Python 3 was not found. Install it from python.org, then run this script again.'
}

Write-Host '=== ICH Windows setup ==='
Write-Host "Python: $($Python.Source)"

if (-not (Test-Path -LiteralPath (Join-Path $Venv 'Scripts\python.exe'))) {
    & $Python.Source -m venv $Venv
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create virtual environment (exit $LASTEXITCODE)."
    }
}

$VenvPython = Join-Path $Venv 'Scripts\python.exe'
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Could not upgrade pip (exit $LASTEXITCODE)."
}
& $VenvPython -m pip install -r (Join-Path $Root 'requirements-windows.txt')
if ($LASTEXITCODE -ne 0) {
    throw "Could not install Windows requirements (exit $LASTEXITCODE)."
}

Write-Host ''
Write-Host 'Python USB layer installed.'
Write-Host 'Test it with:'
Write-Host '  .\.venv\Scripts\python.exe .\windows\status.py'
Write-Host ''
Write-Warning 'Windows must expose the Apple DFU/Recovery interface through WinUSB/libusb.'
Write-Warning 'If status.py reports Access denied, use Zadig only for the currently connected'
Write-Warning 'Apple DFU (05AC:1227) or Apple Recovery (05AC:1280-1283) device. Do not replace'
Write-Warning 'the normal-mode Apple Mobile Device USB driver.'
