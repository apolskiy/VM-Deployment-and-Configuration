# Inspect or reconcile the local encrypted manifest with the Go utility.
# Builds the utility on demand and resolves the key and manifest paths from the
# active configuration (config/cluster.toml plus any cluster.local.toml overlay),
# so nothing is hard-coded to a particular machine or user.
#
# Examples:
#   scripts\manifest.ps1 show
#   scripts\manifest.ps1 show -Format json
#   scripts\manifest.ps1 mark-removed
[CmdletBinding()]
param(
    [Parameter(Position = 0)] [ValidateSet("show", "mark-removed")] [string] $Action = "show",
    [string] $Format = "table"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONPATH = "src"

    # Resolve the configured key and manifest paths (overlay-aware) via a helper
    # script, avoiding the quote-mangling PowerShell applies to inline
    # `python -c` arguments passed to a native executable.
    $paths = python scripts\_resolve_inventory_paths.py
    if ($LASTEXITCODE -ne 0) { throw "could not read configuration" }
    $key, $file = $paths -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }

    $binary = Join-Path $env:TEMP "vmdeploy-inventory.exe"
    Push-Location (Join-Path $repo "goservice")
    try {
        go build -o $binary .
        if ($LASTEXITCODE -ne 0) { throw "go build failed" }
    }
    finally { Pop-Location }

    $cliArgs = @("manifest", $Action, "-key", $key, "-file", $file)
    if ($Action -eq "show") { $cliArgs += @("-format", $Format) }
    & $binary @cliArgs
    exit $LASTEXITCODE
}
finally { Pop-Location }
