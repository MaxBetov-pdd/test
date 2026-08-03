[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('setup', 'status', 'build', 'boot', 'iproxy')]
    [string]$Command = 'status',

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$CommandArgs
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

if ($Command -eq 'setup') {
    & (Join-Path $PSScriptRoot 'setup.ps1') @CommandArgs
    exit $LASTEXITCODE
}

$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Windows environment is not installed. Run: powershell -ExecutionPolicy Bypass -File .\windows\setup.ps1'
}

$Script = Join-Path $PSScriptRoot ($Command + '.py')
& $Python $Script @CommandArgs
exit $LASTEXITCODE
