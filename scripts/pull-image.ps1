# Pull the golden OVA from a public OCI registry (published with
# publish-image.ps1) and place it where the configuration expects it. No
# registry login is needed for a public artifact.
#
#   scripts\pull-image.ps1                     # uses [virtualbox].template_image_ref
#   scripts\pull-image.ps1 -Ref ghcr.io/apolskiy/apcluster-golden:latest
[CmdletBinding()]
param(
    [string] $Ref,
    [string] $DestDir,
    [string] $Config = "config/cluster.toml"
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command oras -ErrorAction SilentlyContinue)) {
    throw "The 'oras' CLI is not on PATH. Install it from https://oras.land/docs/installation"
}

# Land the appliance exactly where [virtualbox].template_ova expects it, and
# pull the reference the configuration names, so a registry move is a config
# change rather than a documentation change. The helper prints the path first
# and the reference second.
if (-not $DestDir -or -not $Ref) {
    $resolved = @(python scripts/_resolve_template_ova.py $Config)
    if ($LASTEXITCODE -ne 0 -or $resolved.Count -lt 2) {
        throw "could not resolve template_ova / template_image_ref from $Config"
    }
    if (-not $DestDir) { $DestDir = Split-Path -Parent $resolved[0] }
    if (-not $Ref) { $Ref = $resolved[1] }
}
if (-not $Ref) {
    throw ("No image reference. Set [virtualbox].template_image_ref in $Config " +
           "(or cluster.local.toml), or pass -Ref explicitly.")
}

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
Write-Host "Pulling $Ref into $DestDir (this is a multi-gigabyte download)"
oras pull $Ref -o $DestDir
if ($LASTEXITCODE -ne 0) {
    throw ("oras pull failed for $Ref. If this is a GHCR 401 on a public repository, " +
           "the package itself is still private - make it public in its package settings.")
}
Write-Host "Pulled $Ref into $DestDir"
Write-Host "Now run: python -m vmdeploy.cli provision   (imports it and boots the cluster)"
