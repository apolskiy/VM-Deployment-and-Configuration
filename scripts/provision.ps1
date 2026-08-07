# Import and boot the cluster guests from the golden template, one at a time,
# individualising each (machine-id, SSH host keys, hostname) before the next.
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Extra)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONPATH = "src"
    python -u -m vmdeploy.cli --verbose provision @Extra
    exit $LASTEXITCODE
}
finally { Pop-Location }
