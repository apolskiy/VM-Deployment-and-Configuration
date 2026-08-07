# Run the end-to-end suite against the live cluster and collect Allure results.
# By default endpoints come from config/cluster.toml (hostnames resolve via
# DHCP-registered DNS); pass -BalancerUrl / -InventoryUrl to target IPs instead.
[CmdletBinding()]
param(
    [string] $BalancerUrl = "",
    [string] $InventoryUrl = "",
    [string] $AllureDir = "allure-results",
    [Parameter(ValueFromRemainingArguments = $true)] [string[]] $Extra
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONPATH = "src"
    $cliArgs = @("-m", "pytest", "--require-cluster", "--alluredir=$AllureDir", "-p", "no:cacheprovider")
    if ($BalancerUrl) { $cliArgs += "--balancer-url"; $cliArgs += $BalancerUrl }
    if ($InventoryUrl) { $cliArgs += "--inventory-url"; $cliArgs += $InventoryUrl }
    python @cliArgs @Extra
    exit $LASTEXITCODE
}
finally { Pop-Location }
