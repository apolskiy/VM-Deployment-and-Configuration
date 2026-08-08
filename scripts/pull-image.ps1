# Pull the golden OVA from a public OCI registry (published with
# publish-image.ps1) and place it where the configuration expects it. No
# registry login is needed for a public artifact.
#
#   scripts\pull-image.ps1                     # uses [virtualbox].template_image_ref
#   scripts\pull-image.ps1 -Ref ghcr.io/apolskiy/apcluster-golden:latest
#
# The download is RESUMABLE. The appliance is a single multi-gigabyte blob, and
# `oras pull` cannot resume, so a network fault part way through costs the whole
# transfer. This fetches the blob with curl's byte-range resume instead, then
# verifies the result against the digest the registry advertises, which is the
# same integrity guarantee oras provides internally. Re-run the script after an
# interruption and it continues from the bytes already on disk.
[CmdletBinding()]
param(
    [string] $Ref,
    [string] $DestDir,
    [string] $Config = "config/cluster.toml",
    [int] $Attempts = 3
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command oras -ErrorAction SilentlyContinue)) {
    throw "The 'oras' CLI is not on PATH. Install it from https://oras.land/docs/installation"
}
$curl = (Get-Command curl.exe -ErrorAction SilentlyContinue).Source
if (-not $curl) { throw "curl.exe was not found; it ships with Windows 10 1803 and later." }

# Land the appliance exactly where [virtualbox].template_ova expects it, and
# pull the reference the configuration names, so a registry move is a config
# change rather than a documentation change. The helper prints the path first
# and the reference second.
$ovaName = "apcluster-golden.ova"
if (-not $DestDir -or -not $Ref) {
    $resolved = @(python scripts/_resolve_template_ova.py $Config)
    if ($LASTEXITCODE -ne 0 -or $resolved.Count -lt 2) {
        throw "could not resolve template_ova / template_image_ref from $Config"
    }
    $ovaName = Split-Path -Leaf $resolved[0]
    if (-not $DestDir) { $DestDir = Split-Path -Parent $resolved[0] }
    if (-not $Ref) { $Ref = $resolved[1] }
}
if (-not $Ref) {
    throw ("No image reference. Set [virtualbox].template_image_ref in $Config " +
           "(or cluster.local.toml), or pass -Ref explicitly.")
}

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
$landed = Join-Path $DestDir $ovaName

# 1. Ask the registry what the appliance is: its blob digest and exact size.
Write-Host "Reading the manifest for $Ref"
$manifestJson = oras manifest fetch $Ref 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {
    throw ("Could not read the manifest for $Ref. A 401 on a public repository means the " +
           "package is still private, which is GHCR's default; make it public in its " +
           "package settings. Registry said: $manifestJson")
}
$layer = ($manifestJson | ConvertFrom-Json).layers[0]
$digest = $layer.digest
$expectedBytes = [int64] $layer.size
Write-Host ("Appliance is {0} GB, digest {1}" -f [math]::Round($expectedBytes / 1GB, 2), $digest)

# 2. Work out the blob URL. docker.io is a display name; its API lives elsewhere.
$refNoTag = ($Ref -split ":")[0]
$parts = $refNoTag -split "/", 2
$registry = $parts[0]
$repository = $parts[1]
$apiHost = if ($registry -eq "docker.io") { "registry-1.docker.io" } else { $registry }
$blobUrl = "https://$apiHost/v2/$repository/blobs/$digest"

# 3. Resolve where the blob actually lives, for each attempt.
#
#    Registries answer a blob request with a 307 to a short-lived presigned URL
#    on a CDN. That redirect is what breaks a naive `curl -L -C -`: the range
#    header goes to the registry, which does not honour it, and curl gives up
#    with "server does not seem to support byte ranges" rather than re-applying
#    the range after following. Resolving the redirect here and ranging against
#    the CDN URL directly is what makes resume work. The presigned URL expires,
#    so this is re-resolved on every attempt rather than computed once.
#
#    Authentication is discovered from the WWW-Authenticate challenge rather
#    than hardcoded, so the same code path serves GHCR and Docker Hub.
function Resolve-BlobSource {
    param([string] $Url, [string] $Repository, [string] $CurlPath)

    $token = ""
    $challenge = & $CurlPath -s -o NUL -D - $Url 2>&1 | Out-String
    if ($challenge -match '(?im)^www-authenticate:\s*Bearer\s+(.+)$') {
        $params = @{}
        foreach ($m in [regex]::Matches($Matches[1], '(\w+)="([^"]*)"')) {
            $params[$m.Groups[1].Value] = $m.Groups[2].Value
        }
        if ($params.ContainsKey("realm")) {
            $tokenUrl = $params["realm"] + "?service=" + [uri]::EscapeDataString($params["service"])
            $scope = if ($params.ContainsKey("scope")) {
                $params["scope"]
            } else {
                "repository:${Repository}:pull"
            }
            $tokenUrl += "&scope=" + [uri]::EscapeDataString($scope)
            $token = (Invoke-RestMethod -Uri $tokenUrl).token
        }
    }

    $authArgs = @()
    if ($token) { $authArgs = @("-H", "Authorization: Bearer $token") }

    # Follow exactly one hop by hand so the range lands on the object itself.
    $headers = & $CurlPath -s -o NUL -D - @authArgs $Url 2>&1 | Out-String
    $location = ([regex]::Match($headers, '(?im)^location:\s*(\S+)')).Groups[1].Value
    if ($location) {
        # Presigned: it carries its own credentials, so no header is sent on.
        return @{ Url = $location; Args = @() }
    }
    return @{ Url = $Url; Args = $authArgs }
}

# 4. Fetch with resume. -C - continues from whatever is already on disk, so an
#    interrupted run is re-entrant: running the script again finishes the job.
for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
    $have = if (Test-Path $landed) { (Get-Item $landed).Length } else { 0 }
    if ($have -ge $expectedBytes) { break }
    if ($have -gt 0) {
        Write-Host ("Resuming from {0} GB of {1} GB" -f
            [math]::Round($have / 1GB, 2), [math]::Round($expectedBytes / 1GB, 2))
    } else {
        Write-Host "Downloading $ovaName to $DestDir"
    }

    $source = Resolve-BlobSource -Url $blobUrl -Repository $repository -CurlPath $curl
    & $curl -C - --retry 5 --retry-delay 5 --retry-all-errors `
        --progress-bar @($source.Args) -o $landed $source.Url
    if ($LASTEXITCODE -eq 0) { break }

    if ($attempt -eq $Attempts) {
        $got = if (Test-Path $landed) { (Get-Item $landed).Length } else { 0 }
        throw ("Download of $Ref failed after $Attempts attempt(s) with " +
               "$([math]::Round($got / 1GB, 2)) GB of " +
               "$([math]::Round($expectedBytes / 1GB, 2)) GB retrieved. The partial file is " +
               "kept, so running this script again resumes rather than restarting.")
    }
    Write-Host "Transfer interrupted; retrying ($($attempt + 1) of $Attempts)."
}

# 5. Verify. Resume appends to a file this script did not write in one pass, so
#    the digest check is what proves the appliance is intact before it is ever
#    imported. A corrupt file is removed rather than left to fail obscurely in
#    'provision'.
$actualBytes = (Get-Item $landed).Length
if ($actualBytes -ne $expectedBytes) {
    throw "Downloaded $actualBytes bytes but the registry advertises $expectedBytes. Re-run to resume."
}
Write-Host "Verifying the appliance against its digest (this reads the whole file)"
$actualDigest = "sha256:" + (Get-FileHash $landed -Algorithm SHA256).Hash.ToLower()
if ($actualDigest -ne $digest) {
    [System.IO.File]::Delete($landed)
    throw ("Digest mismatch: expected $digest but got $actualDigest. The corrupt file has " +
           "been deleted so it cannot be imported. Run this script again.")
}

Write-Host ""
Write-Host ("Pulled $Ref into $DestDir ({0} GB, digest verified)" -f
    [math]::Round($actualBytes / 1GB, 2))
Write-Host "Now run: python -m vmdeploy.cli provision   (imports it and boots the cluster)"
