# Destroy the cluster guests, leaving the golden template intact.
# Pass -Force to skip the confirmation prompt (forwards --yes to the CLI).
[CmdletBinding()]
param(
    [switch] $Force,
    [Parameter(ValueFromRemainingArguments = $true)] [string[]] $Extra
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONPATH = "src"
    $cliArgs = @("teardown")
    if ($Force) { $cliArgs += "--yes" }
    python -m vmdeploy.cli @cliArgs @Extra
    exit $LASTEXITCODE
}
finally { Pop-Location }
