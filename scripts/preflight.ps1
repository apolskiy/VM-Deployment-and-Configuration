# Check whether this host has enough RAM, disk, tools, and keys to deploy or
# tear down the configured cluster. Run this BEFORE deploy or teardown.
# Exits non-zero if any check fails, so it can gate an automated pipeline.
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Extra)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONPATH = "src"
    python -m vmdeploy.cli preflight @Extra
    exit $LASTEXITCODE
}
finally { Pop-Location }
