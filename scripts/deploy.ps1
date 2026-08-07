# Full cluster deploy: golden template export, provision, then configure.
# Runs from anywhere; resolves the repo root from this script's location so it
# is safe to double-click or call from a scheduler.
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Extra)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONPATH = "src"
    python -u -m vmdeploy.cli --verbose deploy @Extra
    exit $LASTEXITCODE
}
finally { Pop-Location }
