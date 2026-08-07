# Publish the golden OVA to a container registry as a public OCI artifact, so
# anyone can pull and import it — no private sharing needed. Uses ORAS
# (https://oras.land). Log in first:  oras login docker.io -u <user>
#
#   scripts\publish-image.ps1 -Ref docker.io/<youruser>/apcluster-golden:latest
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Ref,
    [string] $OvaPath,
    [string] $Config = "config/cluster.toml"
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command oras -ErrorAction SilentlyContinue)) {
    throw "The 'oras' CLI is not on PATH. Install it from https://oras.land/docs/installation"
}

# Resolve the OVA from the configuration rather than assuming a location, so
# this cannot drift from [virtualbox].template_ova the way a hardcoded default
# silently did.
if (-not $OvaPath) {
    $OvaPath = (python scripts/_resolve_template_ova.py $Config | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0 -or -not $OvaPath) { throw "could not resolve template_ova from $Config" }
}
if (-not (Test-Path $OvaPath)) {
    throw "OVA not found at '$OvaPath'. Build it first: python -m vmdeploy.cli template"
}

$ovaFile = Split-Path -Leaf $OvaPath
Push-Location (Split-Path -Parent $OvaPath)
try {
    # Push the appliance as an OCI artifact with a descriptive media type.
    oras push $Ref --artifact-type application/vnd.virtualbox.ova `
        "${ovaFile}:application/x-tar"
    if ($LASTEXITCODE -ne 0) { throw "oras push failed" }
    Write-Host "Published $OvaPath to $Ref"
    Write-Host "Anyone can now fetch it with: scripts\pull-image.ps1 -Ref $Ref"
}
finally { Pop-Location }
