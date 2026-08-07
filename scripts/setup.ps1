# First-run setup: generate your own keys, create a hardened operational user on
# the template, verify it, disable the stock bootstrap login, and record the new
# identity in the gitignored config/cluster.local.toml.
#
# Run this once on a new machine, after copying cluster.local.toml.example to
# cluster.local.toml and setting your bootstrap [ssh] user + key_path.
#
#   scripts\setup.ps1                          # create 'vmadmin'
#   scripts\setup.ps1 --new-user qa1           # choose the operational account
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Extra)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONPATH = "src"
    python -u -m vmdeploy.cli --verbose setup @Extra
    exit $LASTEXITCODE
}
finally { Pop-Location }
