# Re-enable a previously disabled account on the TEMPLATE box after the hardened
# image has been exported. The exported OVA and its clones are unaffected and
# remain hardened; this only restores access on the working template VM itself
# (useful when that box is also used for other testing).
#
#   scripts\restore-bootstrap.ps1 -User apolskiy -PubKeyFile ~\.vmdeploy\keys\apolskiy.pub
#   scripts\restore-bootstrap.ps1 -User apolskiy -PubKeyFile <path> -LeaveRunning
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $User,
    [Parameter(Mandatory = $true)] [string] $PubKeyFile,
    [switch] $LeaveRunning
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONPATH = "src"
    $cliArgs = @("--verbose", "restore-bootstrap", "--user", $User, "--pubkey-file", $PubKeyFile)
    if ($LeaveRunning) { $cliArgs += "--leave-running" }
    python -u -m vmdeploy.cli @cliArgs
    exit $LASTEXITCODE
}
finally { Pop-Location }
