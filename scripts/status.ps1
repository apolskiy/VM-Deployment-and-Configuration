# Report cluster VM state and addresses. Exit code is non-zero unless every
# guest is running, so it doubles as a health gate in CI or a scheduled check.
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Extra)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONPATH = "src"
    python -m vmdeploy.cli status @Extra
    exit $LASTEXITCODE
}
finally { Pop-Location }
