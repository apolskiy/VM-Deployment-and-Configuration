# Configure Apache (balancer + backends), deploy the Go inventory service, and
# write the manifest. Safe to re-run: every step is idempotent.
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Extra)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONPATH = "src"
    python -u -m vmdeploy.cli --verbose configure @Extra
    exit $LASTEXITCODE
}
finally { Pop-Location }
