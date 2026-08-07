# VM-Deployment-and-Configuration

Deploys a cluster of headless Ubuntu Linux VMs on a Windows host with
VirtualBox, from a single golden image. It configures Apache as a load balancer
in front of two static web servers, serves an AES-256-GCM encrypted inventory
of the deployed hosts from a Go service, and validates the whole thing end to
end.

It is built to be a **deterministic, QA-ready harness**: one golden image with
the toolchain and app baked in, deploys that install nothing over the network,
a self-checking configure step that recompiles only on real source changes, and
a Playwright/Allure suite that exercises the running cluster (load balancing,
encrypted inventory, and — see *HA testing* — failover).

Automation is Python; the inventory service is Go; validation is pytest with
Playwright and Allure.

---

## Quickstart

Two ways to run this. Pick by what you want.

### A. Just see it work — containers, ~2 minutes, nothing to install but Docker

No VirtualBox, no image download, no secrets. Works on Linux/macOS, or Windows
**if** Hyper-V is enabled (which is mutually exclusive with VirtualBox — see the
note under *Try it in containers*).

```bash
git clone https://github.com/apolskiy/VM-Deployment-and-Configuration.git
cd VM-Deployment-and-Configuration
python -m pip install -r requirements.txt

docker compose up --build -d
PYTHONPATH=src python -m pytest --require-cluster \
    --balancer-url http://localhost --inventory-url http://localhost:5090
docker compose down -v
```

### B. The real thing — three Ubuntu VMs on a Windows host with VirtualBox

```powershell
# 0. Prerequisites: VirtualBox 7.x, Python 3.11+, Go 1.26+, ~28 GB free, 8 GB RAM.
git clone https://github.com/apolskiy/VM-Deployment-and-Configuration.git
cd VM-Deployment-and-Configuration
python -m pip install -r requirements.txt

# 1+2. Generate YOUR keys and get the image. Two routes — pick one.
#
#   Route A: use the published image (no Ubuntu VM of your own needed).
python -m vmdeploy.cli setup --keys-only  #    keys only; nothing to harden yet
scripts\pull-image.ps1                    #    ~7 GB download
#
#   Route B: bake your own image from an Ubuntu VM you already have.
python -m vmdeploy.cli setup              #    keys + harden that VM's login
python -m vmdeploy.cli template           #    bake and export the golden OVA

# 3. Check the host can actually take it (RAM, disk, tools, keys).
python -m vmdeploy.cli preflight

# 4. Boot and configure the cluster.
python -m vmdeploy.cli provision
python -m vmdeploy.cli configure

# 5. Prove it works.
python -m pytest --require-cluster --alluredir=allure-results
allure generate allure-results --clean -o allure-report
allure open allure-report

# 6. When finished.
python -m vmdeploy.cli teardown --yes
```

Steps 3–4 are also available as one command: `python -m vmdeploy.cli deploy`
(route B only — `deploy` rebuilds the image, which route A does not have the
source VM for).

**Why `--keys-only` exists:** plain `setup` hardens a template VM you own, so it
needs one to exist. If you pulled the image there is nothing to harden — but you
still need the keypair whose public half goes into each guest's seed, and the
AES key for the inventory. `--keys-only` generates exactly those and stops,
contacting nothing.

**What you end up with:** `http://apjump/` load-balances across `apnode1` and
`apnode2`; `http://apjump:5090/clusterview` shows the decrypted inventory in a
browser. Both are also reachable at the jump station's IP, which
`vmdeploy.cli status` prints.

**If you only have one machine and it runs VirtualBox**, use path B — the
container path cannot run alongside VirtualBox on Windows.

---

## Architecture

```
Windows host  (VirtualBox 7.2.14)
│
├── apubuntuD ......... golden template SOURCE (off while the cluster runs on
│                       this 16 GB host — see note below). `template` bakes
│                       Apache + pinned Go + the inventory binary in, exports:
│                            │
│                            │  VBoxManage export → apcluster-golden.ova
│                            ▼
├── apjump ............ JUMP STATION
│     ├── Apache mod_proxy_balancer          :80
│     │     balancer://apcluster (lbmethod=byrequests, no stickysession)
│     └── Go inventory service               :5090
│           /clusterview  /api/inventory  /api/inventory/raw  /healthz
│
├── apnode1 ........... BACKEND  Apache :8080 → your static site build
└── apnode2 ........... BACKEND  Apache :8080 → your static site build
```

`apubuntuD` is the golden-image source: a prepared Ubuntu VM that `template`
bakes dependencies into and exports as `apcluster-golden.ova`. Every cluster
guest is a clone of that image, and takes its identity — hostname, SSH host
keys, machine ID, and the only account key that will work — from a small
cloud-init seed attached at import. The shared image itself holds none of that.

### How a request is traced back to a backend

Each backend is stamped with its identity two independent ways:

| Layer   | Mechanism                                 | Proves                           |
| ------- | ----------------------------------------- | -------------------------------- |
| HTTP    | `X-Backend-Host` header via `mod_headers` | Which member answered            |
| Browser | `<meta name="x-backend-host">` in HTML    | Which member's page was rendered |

The header survives on any resource; the meta tag is visible to the Playwright
DOM. Testing both means a proxy cache cannot make a single-member pool look
balanced.

---

## Try it in containers (no VirtualBox)

There are two deployment targets that build the same cluster and pass the same
E2E suite:

- **VirtualBox** — real Ubuntu VMs from a golden image (the rest of this README).
- **Docker Compose** — the same load balancer, two backends, and inventory
  service as containers. Nothing to install but Docker, and it runs in CI.

Anyone who clones the repo can bring the whole cluster up and verify it:

```bash
docker compose up --build -d

# Browse http://localhost/ (load-balanced site) and
# http://localhost:5090/clusterview (encrypted inventory, decrypted view).

# Or run the full end-to-end suite against it:
PYTHONPATH=src python -m pytest --require-cluster \
    --balancer-url http://localhost --inventory-url http://localhost:5090

docker compose down -v
```

The stack uses the same hostnames (`apjump`, `apnode1`, `apnode2`) and the same
encrypted-inventory wire format as the VM path, so the identical test suite
validates both. GitHub Actions runs this on every push — see
`.github/workflows/ci.yml`.

> **Windows note:** you cannot run this container stack and the VirtualBox path
> on the same Windows host at the same time. Docker Desktop's Hyper-V/WSL2
> backend and VirtualBox contend for the hypervisor — enabling Hyper-V for
> Docker stops VirtualBox from running, and vice versa. On a VirtualBox host,
> leave Hyper-V disabled and let **GitHub Actions** (Linux runners, native
> Docker, no conflict) validate the container path. Run containers locally only
> on Linux/macOS or a separate machine.

### Sharing the VM image publicly

The golden OVA is published to an OCI registry as an ordinary artifact, so a
multi-gigabyte appliance never has to be shared privately:

```powershell
# Pull the published image (no login needed for a public artifact) and deploy it.
scripts\pull-image.ps1
python -m vmdeploy.cli provision
```

Both scripts read the reference from **`[virtualbox].template_image_ref`** in
`config/cluster.toml`, so no command line carries a registry name. That is
deliberate: registries get retired and accounts get cleaned up, so moving the
image — or republishing it rebuilt on a newer Ubuntu — is a one-line config
change that every script follows, instead of an edit to documentation that
readers may have already copied. Pass `-Ref` to override for a one-off.

Publishing (only the maintainer needs this):

```powershell
# A token, never an account password. GitHub disabled password auth in 2021.
#   gh auth refresh -h github.com -s write:packages,delete:packages
#   gh auth token | oras login ghcr.io -u <youruser> --password-stdin
scripts\publish-image.ps1
```

> **A published GHCR package is private by default — repository visibility does
> not propagate to packages.** Until it is made public once in the package's
> settings, an anonymous `pull-image.ps1` fails with a `401`, which looks like a
> broken project rather than a permissions setting. `pull-image.ps1` says so in
> its error text if you hit it.

**The image is safe to publish because it contains no credentials at all.**
Sanitisation strips every cached registry, git, and `gh` credential, locks every
account, and removes **every `authorized_keys`, including the builder's own**.
A downloaded appliance therefore has no usable login — verified by booting the
exported OVA with no seed attached and confirming SSH answers
`Permission denied (publickey,password)` for the builder's own key.

Access is created at deploy time instead: `provision` writes a per-guest
cloud-init NoCloud seed carrying *your* public key and attaches it as a virtual
CD, and the guest configures itself on first boot. No shared secret ever exists,
so publishing the image grants nobody anything. See
*[First-boot identity](#first-boot-identity-how-a-credential-free-image-becomes-your-cluster)*.

The transport is CI-verified: the `publish-roundtrip` job runs both scripts
against GHCR on every push and asserts the pulled artifact is byte-identical to
the published one.

If you only want to see the project work, the container path above needs no
image and no download at all.

---

## The golden image: deterministic, drift-free deploys

The slow, non-deterministic part of any VM deploy is installing packages over
the network at deploy time. This project moves that work into a **one-time
image build** so deploys are fast, reproducible, and independent of package
mirror state.

**`template` (build) bakes into `apubuntuD` before export:**

- **Apache** (`apt`), so no web-server install happens per deploy.
- **A pinned Go toolchain.** The exact archive (`go1.26.5.linux-amd64.tar.gz`)
  is downloaded **on the automation host** (whose internet is reliable),
  verified against the SHA-256 in `config/cluster.toml`, cached, then pushed to
  the guest over SFTP and re-verified there before extraction. Guests reach
  large go.dev downloads unreliably over the bridged link, so pulling on the
  guest stalled; fetching on the host and pushing over the LAN is both fast and
  dependable. A corrupted or substituted archive fails closed. Pinning a
  version (rather than the distro package) makes every rebuild identical.
- **A precompiled `inventory` binary**, tagged with a **source fingerprint**: a
  SHA-256 over every Go file that determines the binary, written to
  `/opt/inventory/.source-hash`.

**`configure` is self-checking and only updates on real change.** It compares
the local Go-source fingerprint against the one baked into the image:

- **Match** → the baked binary is authoritative; **nothing is compiled**.
- **Differ** (you edited the Go service since the image was built) → it installs
  the pinned Go and recompiles, then restamps the fingerprint.

So the running system is verified against source on every configure and updated
only when it has genuinely drifted — no unnecessary rebuilds, no silent
staleness. Secrets are never baked in: the AES key and datastore are
cluster-specific and are written to each clone at configure time, not into the
shared image.

---

## First-boot identity: how a credential-free image becomes your cluster

A distributable image and a usable image pull in opposite directions. Anything
baked in that lets *you* log in also lets **everyone who downloads it** log in.
This project resolves that by shipping an image with no credentials whatsoever
and supplying identity per guest at deploy time.

**What the image build removes** (`sanitize_image`, run just before export):

- every cached Docker/registry, git, and `gh` credential, and shell history;
- every SSH key, **including the operational account's `authorized_keys`** — the
  build fails rather than exports if one survives;
- every human account except the operational one, which is left in place but
  has no way in;
- the build tooling itself (`git`, `docker`, `gh`, `/var/lib/docker`), because
  a runtime image has no use for it and it is what accumulates credentials.

**What the image build arms** (`arm_cloud_init`):

Ubuntu's installer disables cloud-init after first boot and pins
`datasource_list: [None]` in `/etc/cloud/cloud.cfg.d/99-installer.cfg`. Both are
removed, `NoCloud` is pinned explicitly, and `cloud-init clean --logs
--machine-id` makes the next boot a genuine first boot. Ordering matters here:
`clean` runs the hooks in `/etc/cloud/clean.d`, which delete drop-in
configuration, so vmdeploy's drop-ins are written *after* the clean — including
one that re-disables cloud-init's network handling, because the installer's
version of that setting is itself removed by the clean.

**What `provision` supplies per guest** (`vmdeploy.seed`):

A ~66 KB ISO labelled `cidata` holding `user-data` and `meta-data`, attached to
the guest's optical drive before it is ever started. It carries the hostname,
a unique instance id, and **your** public key. On first boot cloud-init creates
the account with that key, sets the hostname and `/etc/hosts`, and regenerates
SSH host keys so no two clones share an identity.

```
      golden OVA (no credentials, cannot be logged into)
                       +
      per-guest seed ISO  ──  hostname + instance-id + YOUR public key
                       ↓
             a guest only you can reach
```

Because the key is injected rather than shared, publishing the image grants
nobody access, and two people deploying the same image get clusters neither can
reach in the other's environment. It also removed a whole provisioning step: the
old flow booted a clone, logged in over SSH to rename it, and rebooted. Identity
now exists before first boot, so that pass and its reboot are gone.

---

## Encrypted inventory and the manifest

The registry is not merely encoded — base64 is an encoding, not a cipher. It is
protected with authenticated encryption:

```
base64( nonce[12] || ciphertext || tag[16] )     plaintext = UTF-8 JSON array
```

That framing is exactly what Go's `gcm.Seal(nonce, nonce, plaintext, nil)`
emits and what Python's `AESGCM.encrypt` produces with the nonce prepended, so
the Go service and Python client interoperate byte for byte. The contract is
pinned by `goservice/interop_test.go`, which decrypts a fixture generated by the
Python client. GCM is authenticated, so a wrong key, a truncated file, or a
flipped bit all raise `DecryptionError` rather than returning plausible garbage.

### The manifest is the durable record

The jump station serves a live copy of the registry, but that VM is destroyed on
teardown. The **canonical manifest** is therefore a local encrypted file on the
automation host (`inventory.manifest_file`, encrypted with the same key). Both
lifecycle commands rewrite it, so it never goes stale:

- **deploy / configure** → writes the manifest with hosts `Deployed` / `Active`.
- **teardown** → rewrites it with every host `Removed` / `Inactive`, preserving
  last-known addresses for the audit trail.

### Inspecting the manifest with the Go utility

The same Go binary that serves the registry doubles as an offline manifest tool
(a `manifest` subcommand), reusing the identical AES-256-GCM `Store`, `Record`
type, and wire format — so the file the service serves live and the file the
utility reads are the same contract, and the Go tool decrypts what the Python
side wrote:

```
inventory manifest show         -key KEY -file MANIFEST [-format table|json]
inventory manifest mark-removed -key KEY -file MANIFEST
```

`scripts/manifest.ps1` builds the utility on demand and wraps it, defaulting the
key and manifest paths from the configuration:

```powershell
scripts\manifest.ps1 show                 # aligned table
scripts\manifest.ps1 show -Format json    # machine-readable
scripts\manifest.ps1 mark-removed         # reconcile a half-finished teardown
```

Because it shares the service's sources, the baked linux-amd64 build inspects a
manifest on a guest, while the same source compiled locally inspects the local
manifest on the Windows host.

### Service endpoints

| Method | Path                  | Purpose                                    |
| ------ | --------------------- | ------------------------------------------ |
| GET    | `/healthz`            | Liveness; does not touch the datastore     |
| GET    | `/clusterview`        | HTML table of the decrypted registry       |
| GET    | `/api/inventory`      | Decrypted registry as JSON                 |
| GET    | `/api/inventory/raw`  | The datastore as stored, still encrypted   |
| POST   | `/api/inventory`      | Insert or replace one host record          |

`/api/inventory` and `/api/inventory/raw` are tested as a pair: the first proves
the service can decrypt, the second proves what is on disk is genuinely
ciphertext. Either alone is insufficient.

---

## Repository layout

```
config/cluster.toml           Committed base: topology + toolchain. No secrets.
config/cluster.local.toml.example   Template an engineer copies for their creds
src/vmdeploy/
  config.py                   Typed TOML loading with cross-section validation
  exceptions.py               Exception hierarchy rooted at VmDeployError
  ssh_client.py               Context-managed Paramiko wrapper (agent-aware)
  virtualbox.py               VBoxManage wrapper; LAN-aware guest IP discovery
  provision.py                Template build/export, VM import + seeded first boot
  seed.py                     cloud-init NoCloud seed ISO builder (per-guest identity)
  apache.py                   Balancer and backend vhost rendering + service control
  website.py                  Site retrieval and per-backend identity stamping
  inventory.py                AES-256-GCM client, key management, record upsert
  inventory_service.py        Go build/deploy, image baking, source-hash gate,
                              systemd unit, datastore seeding, local manifest
  preflight.py                Host RAM/disk/tool/key readiness checks
  setup.py                    Keygen, hardened-user creation, stock-login disable
  cli.py                      Subcommand orchestration
goservice/                    Go inventory service + `manifest` subcommand utility
  main.go                     HTTP service + subcommand dispatch
  crypto.go                   AES-256-GCM (shared by service and utility)
  inventory.go                Record type + concurrency-safe encrypted Store
  manifest.go                 Offline manifest CLI (show / mark-removed)
docker/                       Container images: backend, jump (LB), inventory, seeder
docker-compose.yml            Container deployment target (same cluster, no VirtualBox)
.github/workflows/ci.yml      Unit gates + E2E against the container stack
scripts/                      PowerShell + .bat wrappers (see below)
tests/
  test_config.py              Configuration validation (no cluster)
  test_inventory_crypto.py    Encryption contract (no cluster)
  test_manifest.py            Local manifest deploy/teardown lifecycle (no cluster)
  test_source_hash.py         Recompile-gate fingerprint (no cluster)
  test_preflight.py           Preflight checks + portable path expansion (no cluster)
  test_setup.py               Keygen + local-overlay identity (no cluster)
  test_destroy_retry.py       Verified VM deletion / retry (no cluster)
  test_ip_selection.py        LAN-aware guest IP selection (no cluster)
  test_load_balancing.py      E2E distribution, HTTP + browser
  test_inventory_service.py   E2E encryption at rest and retrieval
```

---

## Setup

### Prerequisites

- Windows host with **VirtualBox 7.2+** and `VBoxManage` on disk
- **Python 3.11+** (3.14 in use here)
- A template VM registered with VirtualBox, running Ubuntu with **Guest
  Additions installed**, and **one bootstrap account you can already SSH into
  with a key** — the account you created when you installed Ubuntu, or one from
  a base image shared privately by your team. It only has to work once.

### First-time setup for a new engineer

**No usernames or keys are stored in this public repository.** You bring your
own bootstrap access; the `setup` command generates everything else and hardens
the image. A fresh engineer on a fresh Windows box does this:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium

# 1. Point the tool at YOUR bootstrap account (gitignored, never published)
copy config\cluster.local.toml.example config\cluster.local.toml
#    …then edit it: set [ssh].user and key_path to your bootstrap login.

# 2. Generate your own unique keys + a hardened 'vmadmin' user on the template,
#    verify it works, then disable the bootstrap login. Writes the new identity
#    back into cluster.local.toml.
python -m vmdeploy.cli setup          # or: scripts\setup.ps1

# 3. Build the golden image and deploy — now as the hardened user.
python -m vmdeploy.cli deploy
```

After `setup`, every deployed guest carries only *your* `vmadmin` account and
*your* keys; the stock bootstrap login is locked out. Two engineers on two
machines end up with entirely independent credentials, none of which ever touch
the repo. See **Security model** below.

### Configure

`config/cluster.toml` is the committed base. It holds **no secrets and no
account name** — only the pinned toolchain and portable paths. Anything
personal or machine-specific goes in `config/cluster.local.toml` (gitignored),
which is deep-merged over the base; `VMDEPLOY_SSH_USER` overrides the user for a
one-off run.

**Paths are portable.** They use `~` and environment variables such as
`%USERPROFILE%` and `%VBOX_MSI_INSTALL_PATH%`, which the loader expands, so the
shipped file works unchanged on any machine. An undefined variable is rejected
up front rather than silently resolving to the wrong path.

```toml
# config/cluster.toml (committed — neutral defaults only)
[ssh]
user     = "vmadmin"                 # operational account created by `setup`
key_path = "~/.vmdeploy/keys/vm_key"

[virtualbox]
vboxmanage   = "%VBOX_MSI_INSTALL_PATH%VBoxManage.exe"  # Linux: /usr/bin/VBoxManage
template_vm  = "apubuntuD"                              # baked + exported
template_ova = "~/.vmdeploy/apcluster-golden.ova"

[build]
go_version = "1.26.5"
go_sha256  = "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"
```

Validation runs before any hypervisor call: duplicate VM names, a template
colliding with a cluster member, a port shared between balancer and inventory,
fewer than two backends, and unexpanded path variables are all rejected up
front.

### Preflight: check the host before you deploy

Provisioning boots several VMs and exports a multi-gigabyte appliance, so it
fails slowly when the host is short on RAM or disk or is missing a tool or key.
**Run preflight first** — `deploy` runs it automatically and aborts on any
hard failure (override with `--skip-preflight`):

```powershell
scripts\preflight.ps1          # or: python -m vmdeploy.cli preflight
```

It reports PASS/WARN/FAIL for VBoxManage, the SSH key, the key/manifest
directory, host RAM (guests × `memory_mb` + headroom vs. available), host disk
(on the VirtualBox machine folder), and whether a golden OVA or the template VM
is available. It exits non-zero on any FAIL, so it also gates CI. Run it before
`teardown` too, to confirm the environment is intact. Note that with the cluster
already running, the RAM check will report the running guests' memory as in use —
tear the cluster down before a fresh deploy.

---

## Security model

The repository is safe to publish because it contains automation, not
credentials — the same principle as any public Terraform or Ansible project.

- **No secrets in git.** `.gitignore` blocks private keys (`vm_key*`, `*.pem`,
  `id_rsa*`), the AES inventory key and manifest (`*.key`, `*.enc`), and the
  local overlay (`cluster.local.toml`). The committed `cluster.toml` carries a
  neutral `vmadmin` default, never a real account name — so crawlers scraping
  the repo find nothing to target, hashed or otherwise. **Anything committed to
  a public repo is downloadable forever, including from history, so keys are
  never committed under any encoding.**
- **Keys are generated locally, per install.** `vmdeploy setup` creates a unique
  Ed25519 keypair and AES-256 key on each engineer's own machine, under a
  gitignored directory. Two engineers share no key material.
- **The stock login is disabled.** `setup` creates a dedicated operational user
  on the template, **verifies key-based login and sudo work**, and only then
  locks the bootstrap account (password locked, shell set to `nologin`, removed
  from sudo, authorised keys revoked). The verify-before-disable ordering makes
  lockout impossible. Because this happens on the template before `template`
  bakes the image, every deployed guest ships hardened.
- **CI credentials.** For GitHub Actions, store the key and any password as
  encrypted **Actions Secrets** and inject them at runtime (`VMDEPLOY_SSH_USER`,
  `VMDEPLOY_SUDO_PASSWORD`, a key written to a runner-local path). Secrets are
  never downloadable as repository files. (The E2E tier still needs a host that
  can run VirtualBox; see *Known limitations*.)
- **The golden image ships no credentials at all, and no build tooling.** A
  distributable image must not carry the push credentials that build hosts
  accumulate, nor any login. `template` therefore **sanitises the image before
  export**: it removes cached Docker/registry, git, and `gh` credentials, shell
  history, and **every SSH key including the operational account's own
  `authorized_keys`**; locks every human account; and purges `git`, `docker`,
  and `gh` entirely (the runtime cluster needs none of them). The export
  **fails** rather than ships if an `authorized_keys` survives — a surviving key
  would hand every downloader a working login to every guest deployed from it.
  Access is supplied per guest at deploy time from a cloud-init seed instead;
  see *[First-boot identity](#first-boot-identity-how-a-credential-free-image-becomes-your-cluster)*.
  Build and push container/VM images from **CI or a dedicated build host**,
  never from the golden image or a deployed guest.
- **The template box is never altered by a build.** Because that box is often
  also a working machine (with its own docker/git logins), `template` snapshots
  it first and **rolls back** after export — so the box keeps its tooling,
  credentials, and accounts while the exported OVA is clean. The two never mix.

The one credential `setup` itself needs is your **bootstrap** sudo access, used
once. `RemoteHost.sudo()` tries passwordless `sudo -n` first and falls back to
`VMDEPLOY_SUDO_PASSWORD` (env only, never written to disk).

### Re-enabling the bootstrap on a shared template box

`setup` disables the bootstrap account so the *deployed image* ships hardened.
If the template VM is also used for other work and you want that account back on
**the box itself**, `restore-bootstrap` reverses the disable there — unlock,
restore shell and sudo, reinstate the key — without touching the already-exported
OVA or its clones, which stay hardened:

```powershell
scripts\restore-bootstrap.ps1 -User apolskiy -PubKeyFile ~\.vmdeploy\keys\apolskiy.pub -LeaveRunning
```

Run it after `template` has exported the image.

---

## Deployment

Scripts live in `scripts/` and resolve the repo root themselves, so they run
from anywhere (or double-click). Each sets `PYTHONPATH` and calls the CLI.

| Script | CLI equivalent | What it does |
| ------ | -------------- | ------------ |
| `setup.ps1` | `vmdeploy setup` | generate keys, create hardened user, disable stock login (run once) |
| `restore-bootstrap.ps1` | `vmdeploy restore-bootstrap` | re-enable a disabled account on the template box (image stays hardened) |
| `preflight.ps1` | `vmdeploy preflight` | check RAM/disk/tools/keys (run before deploy & teardown) |
| `deploy.ps1` / `deploy.bat` | `vmdeploy deploy` | preflight → template build → provision → configure |
| `provision.ps1` | `vmdeploy provision` | seed + import + boot the 3 guests |
| `configure.ps1` | `vmdeploy configure` | Apache, site, inventory service, manifest |
| `teardown.ps1 -Force` / `teardown.bat` | `vmdeploy teardown --yes` | destroy guests + update manifest |
| `status.ps1` | `vmdeploy status` | VM state, addresses, and live endpoint URLs |
| `e2e-test.ps1` | `pytest --require-cluster` | run the suite against the live cluster |
| `manifest.ps1` | `inventory manifest …` | inspect/reconcile the local manifest (Go tool) |
| `allure-report.ps1` | `allure generate …` | build and open the static Allure report |
| `publish-image.ps1` / `pull-image.ps1` | `oras push` / `oras pull` | publish or fetch the golden OVA (reference from `template_image_ref`) |

```powershell
$env:PYTHONPATH = "src"

# One-time on a new machine: generate keys + hardened user, disable stock login
python -m vmdeploy.cli setup

# Check readiness, then run the full pipeline
python -m vmdeploy.cli preflight
python -m vmdeploy.cli deploy

# Or step by step
python -m vmdeploy.cli template            # bake deps into the template, export OVA
python -m vmdeploy.cli template --export-only   # export as-is, skip baking
python -m vmdeploy.cli provision           # seed + import 3x, boot
python -m vmdeploy.cli configure           # Apache + site + Go service + manifest
python -m vmdeploy.cli status              # state + live endpoint URLs
python -m vmdeploy.cli teardown --yes      # destroy guests, mark manifest Removed
```

### Reaching the cluster and the manifest

After `configure` (and in `status`) the live endpoints are printed using
**hostnames**, because the DHCP-assigned address changes across reboots while
the registered hostname does not:

```
Endpoints:
  Load balancer      http://apjump/
  Inventory (HTML)   http://apjump:5090/clusterview
  Inventory (JSON)   http://apjump:5090/api/inventory
  Local manifest     scripts/manifest.ps1 show
```

- **In a browser on the same network:** open `http://apjump:5090/clusterview`
  (the root `/` redirects there). This renders the live manifest as a webpage.
- **From a script:** `curl http://apjump:5090/api/inventory` for JSON.
- **From off the network / over an untrusted link:** tunnel over SSH, then use
  `localhost` — nothing is exposed publicly:
  ```
  ssh -L 5090:localhost:5090 vmadmin@apjump
  # then browse http://localhost:5090/clusterview
  ```
- **Offline, from the encrypted local copy:** `scripts\manifest.ps1 show`
  (decrypts `manifest_file` with the Go utility; works even when the jump
  station is down).

### What `provision` does per guest, and why serially

Guests are provisioned **one at a time**. The image ships `/etc/machine-id`
blanked so every clone generates its own, but netplan's default
`dhcp-identifier` derives from that id — so booting clones concurrently, before
each has generated one, makes them contend for a single DHCP lease. Each guest
is fully up before its successor starts:

1. Destroy any existing VM of that name, import the OVA, size it, bridge it onto
   the same host interface the template used.
2. **Build a cloud-init seed ISO for this guest and attach it** — hostname,
   unique instance id, and your public key — *before the machine is started*.
3. Start headless, discover the **reachable LAN** address (the template carries
   a `docker0` bridge at 172.17.0.1 that is filtered out), wait for SSH.
4. Wait for `cloud-init status --wait`, then verify the guest reports the
   expected hostname. SSH answering only proves sshd is up; cloud-init may still
   be creating the account, so this turns a race into a clear failure that
   carries the guest's own diagnosis.

There is no second boot. Identity arrives with the medium rather than being
applied over SSH afterwards, which removed a reboot and a reconnect per guest.

---

## Testing

```powershell
$env:PYTHONPATH = "src"

python -m pytest                 # everything
python -m pytest -m "not e2e"    # unit only, no cluster needed
python -m pytest -m loadbalancing
python -m pytest -m inventory
```

Two tiers. Unit tests exercise cryptography, configuration, the manifest, the
recompile-gate fingerprint, and IP selection in process, and always run. E2E
tests need a live cluster and **skip** when one is unreachable, naming the
endpoint. To make skips hard failures in CI:

```powershell
python -m pytest --require-cluster
# or target IPs directly:
python -m pytest --require-cluster --balancer-url http://192.168.1.66 --inventory-url http://192.168.1.66:5090
```

### Allure reporting

First produce results, then either serve them live or build a static report.
The Allure CLI must be on `PATH` (`https://allurereport.org/docs/install/`;
on Windows, `scoop install allure` or `choco install allure`).

```powershell
# 1. Run the suite, writing raw results
python -m pytest --alluredir=allure-results          # or: scripts\e2e-test.ps1

# 2a. Quick look — serve a temporary report and open it
allure serve allure-results

# 2b. Static report — generate a self-contained ./allure-report, then view it
allure generate allure-results --clean -o allure-report
allure open allure-report
```

`scripts\allure-report.ps1` wraps generate + open (`-Serve` for the live view,
`-NoOpen` to only build):

```powershell
scripts\allure-report.ps1            # build ./allure-report and open it
scripts\allure-report.ps1 -Serve     # serve the live report instead
```

Tests are organised by `@allure.epic` / `@allure.feature` / `@allure.story`,
with `allure.step` blocks around each phase. Distribution counts, backend
shares, decrypted inventory, and rendered HTML are attached; uncaught JavaScript
errors are attached automatically by the `js_errors` fixture.

### Error handling under test

| Failure mode              | Handling                                                    |
| ------------------------- | ----------------------------------------------------------- |
| Dynamic page loading      | `domcontentloaded` then a bounded `networkidle` wait; a page that never idles is read as-is rather than timing out |
| JavaScript runtime errors | `pageerror` listener collects them; asserted empty and attached to Allure |
| Decryption failure        | Wrong key, tampered bytes, truncation, and bad base64 each raise `DecryptionError` and are tested separately |
| Schema vs. crypto failure | Authentic ciphertext carrying wrong-shaped JSON raises `InventoryError`, not `DecryptionError` |

### HA / uptime testing

The harness is designed to support resilience testing, and the pieces are in
place: `vmdeploy status` reports per-VM state, the `X-Backend-Host` header
identifies which member served a request, and `VBoxManage controlvm <vm>
poweroff` (or `systemctl stop apache2` on a backend) takes a node down on
demand. A failover test brings one backend down, asserts the balancer keeps
serving 200s from the surviving member, then restores it. Network-timeout
injection (latency/packet loss via `tc`/`pfifo`) exercises the client-side
timeout and retry handling. These build directly on the existing load-balancing
fixtures. *(Failover/latency tests are a planned extension, not yet in the
suite.)*

---

## Quality gates

```powershell
python -m pylint src/vmdeploy tests      # 10.00/10
python -m mypy                           # strict, clean
cd goservice; go vet ./...; go test ./...
```

| Gate                     | Result                                     |
| ------------------------ | ------------------------------------------ |
| Pylint (source + tests)  | **10.00/10**                               |
| mypy `strict`            | **clean**                                  |
| pytest                   | **unit: all pass**; E2E pass against the live VM cluster and the container stack |
| Go vet + test            | **pass**, incl. Python↔Go crypto interop   |
| CI (GitHub Actions)      | unit gates + E2E against Docker Compose on every push |

All Python carries type hints and Google-style docstrings (`Args:`,
`Returns:`, `Raises:`).

---

## Known limitations

- **Memory sizing is environment-specific.** On the 16 GB reference host,
  powering off the template (4 GB) is what funds the three 1 GB guests, so the
  template is shut down during a run and `preflight` will FAIL a fresh deploy
  while the cluster is already up. On a host with ample RAM the template can
  stay running and both can coexist — this is a resource constraint, not a
  requirement of the design. Run `vmdeploy preflight` to see where your host
  stands. Rebuilding the golden image while the cluster runs needs the template
  *and* the three clones resident at once, so tear the cluster down first if
  memory is constrained.
- **The VirtualBox path cannot run in GitHub Actions.** Hosted runners provide
  no nested virtualisation, so `provision` will not work there. This is why the
  **container stack exists**: CI runs the full E2E suite against Docker Compose
  (`.github/workflows/ci.yml`), and the VM path is validated on a VirtualBox
  host. The unit tier (`-m "not e2e"`) runs anywhere.
- **IPv6 is best-effort.** Recorded when a guest reports a global address,
  omitted otherwise; no cluster function depends on it.
- **Each guest's hostname appears twice in `/etc/hosts`**, as
  `127.0.1.1 apjump apjump`. This is cosmetic and deliberately left alone.
  cloud-init renders the distro template `/etc/cloud/templates/hosts.debian.tmpl`,
  whose line is literally `127.0.1.1 {{fqdn}} {{hostname}}`. The cluster uses
  single-label hostnames with no DNS domain, so both placeholders resolve to the
  same string. The only way to change it is to override that template inside the
  golden image — and that file is **shipped by the `cloud-init` package**, so a
  routine package or system update would overwrite it and silently destroy the
  customisation, leaving no error and no signal that anything changed. A
  duplicate alias is valid and resolution is unaffected; a modification that
  decays invisibly on upgrade is worse than the blemish it fixes.
  (`manage_etc_hosts: localhost` is not an escape hatch: that mode writes the
  same `fqdn hostname` pair onto the `127.0.0.1` line instead.)
