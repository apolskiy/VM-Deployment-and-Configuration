# Publish the golden OVA to a container registry as a public OCI artifact, so
# anyone can pull and import it, with no private sharing needed. Uses ORAS
# (https://oras.land).
#
# Log in first (a token, not your account password):
#   GHCR:       oras login ghcr.io      -u <github-user>  --password-stdin   (PAT, write:packages)
#   Docker Hub: oras login docker.io    -u <docker-user>  --password-stdin   (access token)
#
#   scripts\publish-image.ps1                     # uses [virtualbox].template_image_ref
#   scripts\publish-image.ps1 -Ref ghcr.io/you/apcluster-golden:latest
#
# NOTE for GHCR: a package pushed here is PRIVATE until you make it public once,
# in the package's settings page. Until then an anonymous pull fails with a 401.
[CmdletBinding()]
param(
    [string] $Ref,
    [string] $OvaPath,
    [string] $Config = "config/cluster.toml",
    # Repository the image belongs to. Sent as the standard OCI source
    # annotation, which is what attaches the package to that repository on
    # GitHub and gives it a stable, linkable URL there.
    [string] $SourceRepo = "https://github.com/apolskiy/VM-Deployment-and-Configuration"
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command oras -ErrorAction SilentlyContinue)) {
    throw "The 'oras' CLI is not on PATH. Install it from https://oras.land/docs/installation"
}

# Resolve both the OVA and the target reference from configuration rather than
# assuming them, so neither can drift the way a hardcoded default silently did.
# The helper prints the path first and the reference second.
if (-not $OvaPath -or -not $Ref) {
    $resolved = @(python scripts/_resolve_template_ova.py $Config)
    if ($LASTEXITCODE -ne 0 -or $resolved.Count -lt 2) {
        throw "could not resolve template_ova / template_image_ref from $Config"
    }
    if (-not $OvaPath) { $OvaPath = $resolved[0] }
    if (-not $Ref) { $Ref = $resolved[1] }
}
if (-not $Ref) {
    throw ("No image reference. Set [virtualbox].template_image_ref in $Config " +
           "(or cluster.local.toml), or pass -Ref explicitly.")
}
if (-not (Test-Path $OvaPath)) {
    throw "OVA not found at '$OvaPath'. Build it first: python -m vmdeploy.cli template"
}

$sizeGb = [math]::Round((Get-Item $OvaPath).Length / 1GB, 2)
Write-Host "Publishing $OvaPath ($sizeGb GB) to $Ref"
Write-Host "A single blob this size takes a while; a dropped connection restarts it."

$ovaFile = Split-Path -Leaf $OvaPath
Push-Location (Split-Path -Parent $OvaPath)
try {
    # Push the appliance as an OCI artifact with a descriptive media type. The
    # annotations are what make the result presentable: source links the package
    # to the repository, the rest show up on the package page.
    oras push $Ref --artifact-type application/vnd.virtualbox.ova `
        --annotation "org.opencontainers.image.source=$SourceRepo" `
        --annotation "org.opencontainers.image.description=Golden Ubuntu OVA for the VM-Deployment-and-Configuration cluster. Carries no credentials; each guest is keyed at first boot from a cloud-init seed." `
        --annotation "org.opencontainers.image.licenses=MIT" `
        "${ovaFile}:application/x-tar"
    if ($LASTEXITCODE -ne 0) { throw "oras push failed" }
    Write-Host ""
    Write-Host "Published to $Ref"
    Write-Host "Fetch it with: scripts\pull-image.ps1"
    if ($Ref -like "ghcr.io/*") {
        Write-Host ""
        Write-Host "GHCR packages start PRIVATE. Make it public once, or anonymous pulls 401:"
        Write-Host "  https://github.com/users/apolskiy/packages/container/apcluster-golden/settings"
    }
}
finally { Pop-Location }
