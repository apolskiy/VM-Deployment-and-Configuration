# Publish the golden OVA to a container registry as a public OCI artifact, so
# anyone can pull and import it — no private sharing needed. Uses ORAS
# (https://oras.land). Log in first:  oras login docker.io -u <user>
#
#   scripts\publish-image.ps1 -Ref docker.io/<youruser>/apcluster-golden:latest
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Ref,
    [string] $OvaPath = "$env:USERPROFILE\.vmdeploy\apcluster-golden.ova"
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command oras -ErrorAction SilentlyContinue)) {
    throw "The 'oras' CLI is not on PATH. Install it from https://oras.land/docs/installation"
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
