# Pull the golden OVA from a public OCI registry (published with
# publish-image.ps1) and place it where the configuration expects it. No
# registry login is needed for a public artifact.
#
#   scripts\pull-image.ps1 -Ref docker.io/<user>/apcluster-golden:latest
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Ref,
    [string] $DestDir,
    [string] $Config = "config/cluster.toml"
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command oras -ErrorAction SilentlyContinue)) {
    throw "The 'oras' CLI is not on PATH. Install it from https://oras.land/docs/installation"
}

# Land the appliance exactly where [virtualbox].template_ova expects it, so the
# pulled image is the one the tooling then imports.
if (-not $DestDir) {
    $ovaPath = (python scripts/_resolve_template_ova.py $Config | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0 -or -not $ovaPath) { throw "could not resolve template_ova from $Config" }
    $DestDir = Split-Path -Parent $ovaPath
}

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
oras pull $Ref -o $DestDir
if ($LASTEXITCODE -ne 0) { throw "oras pull failed" }
Write-Host "Pulled $Ref into $DestDir"
Write-Host "Import it as your template VM, then run: python -m vmdeploy.cli setup"
