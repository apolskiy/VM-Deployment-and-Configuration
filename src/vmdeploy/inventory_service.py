"""Build and deployment of the Go inventory service on the jump station.

The service is compiled on the jump station rather than cross-compiled on the
automation host, because the automation host is Windows and shipping a
cross-built ELF binary would make the toolchain that produced it invisible to
anyone debugging the guest later.

The encrypted datastore is written directly over SFTP rather than through the
service's own POST endpoint. Seeding is part of provisioning and must succeed
before the service is necessarily running, so the file is the source of truth
and the HTTP API is a read and update surface layered on top of it.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Final

import requests

from vmdeploy.apache import install_apache
from vmdeploy.config import BuildConfig, ClusterConfig, HostConfig
from vmdeploy.exceptions import InventoryError, RemoteCommandError, VmDeployError
from vmdeploy.inventory import (
    InventoryRecord,
    decrypt_records,
    encrypt_records,
    load_key,
    load_or_create_key,
    utc_timestamp,
    upsert,
)
from vmdeploy.ssh_client import RemoteHost

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

SERVICE_NAME: Final[str] = "inventory"
_SERVICE_UNIT: Final[str] = f"/etc/systemd/system/{SERVICE_NAME}.service"
_BUILD_DIR: Final[str] = "/tmp/vmdeploy-goservice"
_APT_TIMEOUT_SECONDS: Final[int] = 900
_BUILD_TIMEOUT_SECONDS: Final[int] = 600

_SOURCE_FILES: Final[tuple[str, ...]] = (
    "go.mod",
    "main.go",
    "crypto.go",
    "inventory.go",
    "manifest.go",
)
_TEMPLATE_REL: Final[str] = "templates/clusterview.html"

# All files whose contents define the compiled binary. Hashing exactly these,
# in this order, gives a stable fingerprint that gates recompilation.
_HASH_FILES: Final[tuple[str, ...]] = (*_SOURCE_FILES, _TEMPLATE_REL)

_GO_INSTALL_ROOT: Final[str] = "/usr/local/go"
_GO_BIN_LINK: Final[str] = "/usr/local/bin/go"
_SOURCE_HASH_FILE: Final[str] = ".source-hash"

# The pinned Go archive is fetched on the automation host (reliable WAN) and
# cached, then pushed to the guest over SFTP. Guests reach large go.dev
# downloads unreliably over the bridged link, so pulling on the guest stalled.
_GO_CACHE_DIR: Final[Path] = Path(tempfile.gettempdir()) / "vmdeploy-cache"
_DOWNLOAD_CHUNK: Final[int] = 1 << 20
_HOST_DOWNLOAD_TIMEOUT_SECONDS: Final[int] = 180


def source_hash(source_dir: Path) -> str:
    """Compute a stable fingerprint of the Go service sources.

    The hash covers every file that determines the compiled binary, so a
    change to any of them, and nothing else, changes the fingerprint. Deploy
    compares this against the value baked into the golden image and recompiles
    only on a mismatch.

    Args:
        source_dir: Local directory holding the Go service sources.

    Returns:
        A hex SHA-256 digest.

    Raises:
        VmDeployError: If any source file is missing.
    """
    digest = hashlib.sha256()
    for name in _HASH_FILES:
        path = source_dir / name
        if not path.is_file():
            raise VmDeployError(f"Go service source missing from {source_dir}: {name}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    """Compute the SHA-256 of a local file.

    Args:
        path: The file to hash.

    Returns:
        The hex digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_DOWNLOAD_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_go_archive_to_host(build: BuildConfig) -> Path:
    """Download and verify the pinned Go archive on the automation host.

    The archive is cached between runs and re-verified on every call, so a
    cached copy is trusted only if its checksum still matches. Downloading on
    the host rather than the guest sidesteps the slow, stalling go.dev pulls
    seen over the guests' bridged link.

    Args:
        build: Toolchain pinning configuration.

    Returns:
        Local path to the verified archive.

    Raises:
        VmDeployError: If the download fails or the checksum does not match.
    """
    archive = f"go{build.go_version}.linux-amd64.tar.gz"
    dest = _GO_CACHE_DIR / archive
    if dest.is_file() and _sha256_file(dest) == build.go_sha256:
        _LOG.info("Using cached Go archive %s", dest)
        return dest

    _GO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    url = f"https://go.dev/dl/{archive}"
    _LOG.info("Downloading %s on the automation host", archive)
    try:
        with requests.get(url, stream=True, timeout=_HOST_DOWNLOAD_TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            with dest.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                    handle.write(chunk)
    except requests.RequestException as exc:
        raise VmDeployError(f"Failed to download {url} on the automation host: {exc}") from exc

    actual = _sha256_file(dest)
    if actual != build.go_sha256:
        dest.unlink(missing_ok=True)
        raise VmDeployError(
            f"Downloaded Go archive {archive} has SHA-256 {actual}, expected "
            f"{build.go_sha256}; refusing a toolchain that may be corrupted or substituted"
        )
    _LOG.info("Verified Go archive %s (%.1f MB)", archive, dest.stat().st_size / 1024**2)
    return dest


def install_go(host: RemoteHost, build: BuildConfig) -> str:
    """Install the pinned Go toolchain, unless the exact version is present.

    The archive is fetched and verified on the automation host, pushed to the
    guest over SFTP, re-verified there, then extracted. Fetching on the host
    keeps the toolchain reproducible and avoids the guests' unreliable path to
    go.dev; the pinned SHA-256 makes a corrupted or substituted archive fail
    closed rather than compiling silently.

    Args:
        host: A connected SSH session to the guest.
        build: Toolchain pinning configuration.

    Returns:
        The version string reported by the installed toolchain.

    Raises:
        VmDeployError: If Go cannot be installed, verified, or run afterwards.
    """
    wanted = f"go version go{build.go_version} "
    current = host.run("go version 2>/dev/null", check=False)
    if current.ok and current.stdout.strip().startswith(wanted.strip()):
        _LOG.info("Pinned Go already present on %s: %s", host.address, current.stdout.strip())
        return current.stdout.strip()

    local_archive = _fetch_go_archive_to_host(build)
    tmp = f"/tmp/{local_archive.name}"
    _LOG.info("Uploading pinned Go %s to %s over SFTP", build.go_version, host.address)
    try:
        host.sftp().put(str(local_archive), tmp)
    except (OSError, VmDeployError) as exc:
        raise VmDeployError(f"Failed to upload Go archive to {host.address}: {exc}") from exc

    # Re-verify on the guest: the host already checked the download, so this
    # only guards against corruption in transit.
    check = host.run(
        f"printf '%s  %s' {build.go_sha256} {tmp} | sha256sum -c -",
        check=False,
    )
    if not check.ok:
        host.run(f"rm -f {tmp}", check=False)
        raise VmDeployError(
            f"Go archive on {host.address} failed SHA-256 verification after upload; "
            f"the transfer may be corrupt.\n{check.stdout.strip() or check.stderr.strip()}"
        )

    host.sudo(f"rm -rf {_GO_INSTALL_ROOT}")
    host.sudo(f"tar -C /usr/local -xzf {tmp}")
    host.sudo(f"ln -sf {_GO_INSTALL_ROOT}/bin/go {_GO_BIN_LINK}")
    host.sudo(f"ln -sf {_GO_INSTALL_ROOT}/bin/gofmt /usr/local/bin/gofmt")
    host.run(f"rm -f {tmp}", check=False)

    version = host.run("go version", check=False)
    if not version.ok or not version.stdout.strip().startswith(wanted.strip()):
        raise VmDeployError(
            f"Pinned Go {build.go_version} is not usable on {host.address} after install: "
            f"{version.stdout.strip() or version.stderr.strip()}"
        )
    _LOG.info("Go toolchain on %s: %s", host.address, version.stdout.strip())
    return version.stdout.strip()


def build_service(host: RemoteHost, source_dir: Path, remote_dir: str) -> None:
    """Upload the Go sources and compile the service binary in the guest.

    Args:
        host: A connected SSH session to the jump station.
        source_dir: Local directory holding the Go service sources.
        remote_dir: Installation directory for the compiled binary.

    Raises:
        VmDeployError: If sources are missing or compilation fails.
    """
    missing = [name for name in _SOURCE_FILES if not (source_dir / name).is_file()]
    if missing:
        raise VmDeployError(f"Go service sources missing from {source_dir}: {missing}")

    template = source_dir / "templates" / "clusterview.html"
    if not template.is_file():
        raise VmDeployError(f"Go service template missing: {template}")

    _LOG.info("Uploading Go sources to %s:%s", host.address, _BUILD_DIR)
    host.run(f"rm -rf {_BUILD_DIR} && mkdir -p {_BUILD_DIR}/templates")
    for name in _SOURCE_FILES:
        host.put_bytes((source_dir / name).read_bytes(), f"{_BUILD_DIR}/{name}")
    host.put_bytes(template.read_bytes(), f"{_BUILD_DIR}/templates/clusterview.html")
    # put_bytes installs through sudo, so the staged sources land root-owned;
    # hand them back to the login user or the unprivileged build cannot read them.
    host.sudo(f"chown -R $(id -un):$(id -gn) {_BUILD_DIR}", check=False)

    _LOG.info("Compiling the inventory service on %s", host.address)
    # GOCACHE and GOFLAGS keep the build hermetic: the guest has no module
    # proxy access requirement because the service uses only the standard
    # library, and vendoring nothing avoids a network dependency at build time.
    build = host.run(
        f"cd {_BUILD_DIR} && GOCACHE=/tmp/vmdeploy-gocache GOFLAGS=-mod=mod "
        f"go build -trimpath -o {_BUILD_DIR}/{SERVICE_NAME} .",
        check=False,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    if not build.ok:
        raise VmDeployError(
            f"Failed to compile the inventory service on {host.address}:\n"
            f"{build.stderr.strip() or build.stdout.strip()}"
        )

    host.sudo(f"mkdir -p {remote_dir}")
    host.sudo(f"install -m 0755 {_BUILD_DIR}/{SERVICE_NAME} {remote_dir}/{SERVICE_NAME}")
    host.run(f"rm -rf {_BUILD_DIR}", check=False)

    # Record the fingerprint of exactly what was compiled, so a later deploy can
    # tell whether the installed binary is already current and skip rebuilding.
    digest = source_hash(source_dir)
    host.sudo(
        f"sh -c 'printf %s {digest} > {remote_dir}/{_SOURCE_HASH_FILE}'"
    )
    _LOG.info("Installed %s/%s (source hash %s)", remote_dir, SERVICE_NAME, digest[:12])


def installed_source_hash(host: RemoteHost, remote_dir: str) -> str:
    """Read the source hash recorded beside an installed binary.

    The read goes through sudo because the install directory is mode 2770 and
    owned by the service group, which the login user is not a member of; a plain
    read would fail with permission denied and be indistinguishable from an
    absent hash, wrongly forcing a recompile.

    Args:
        host: A connected SSH session to the guest.
        remote_dir: The service installation directory.

    Returns:
        The recorded hex digest, or an empty string if none is present.
    """
    result = host.sudo(
        f"cat {remote_dir}/{_SOURCE_HASH_FILE} 2>/dev/null", check=False
    )
    return result.stdout.strip()


def ensure_service_binary(host: RemoteHost, config: ClusterConfig, source_dir: Path) -> bool:
    """Ensure the current inventory binary is installed, compiling only if stale.

    The golden image ships a precompiled binary tagged with its source hash. If
    that hash matches the local sources, the baked binary is authoritative and
    no compilation happens. Compilation occurs only when the sources have
    changed since the image was built, which is what keeps deploys both fast and
    drift-free.

    Args:
        host: A connected SSH session to the guest.
        config: The cluster configuration.
        source_dir: Local directory holding the Go service sources.

    Returns:
        True if the service was recompiled, False if the baked binary was reused.

    Raises:
        VmDeployError: If a required compile fails.
    """
    remote_dir = config.inventory.remote_dir
    wanted = source_hash(source_dir)
    installed = installed_source_hash(host, remote_dir)
    # sudo: the 2770 install directory is not traversable by the login user, so
    # a plain test would report the baked binary as absent.
    binary_present = host.sudo(f"test -x {remote_dir}/{SERVICE_NAME}", check=False).ok

    if binary_present and installed == wanted:
        _LOG.info(
            "Inventory binary on %s is current (source hash %s); skipping compile",
            host.address,
            wanted[:12],
        )
        return False

    reason = "no baked binary" if not binary_present else "source changed since image build"
    _LOG.info("Recompiling inventory service on %s (%s)", host.address, reason)
    install_go(host, config.build)
    build_service(host, source_dir, remote_dir)
    return True


def prepare_template(host: RemoteHost, config: ClusterConfig, source_dir: Path) -> None:
    """Bake the toolchain, Apache, and a precompiled binary into the template.

    Run against the template VM before it is exported. Every clone then inherits
    a ready-to-run image, so a deploy installs no packages over the network and
    recompiles the inventory app only when its sources have changed since the
    image was built. This is what makes deploys fast, reproducible, and
    independent of package-mirror state.

    The encryption key and datastore are deliberately not baked in: they are
    secret and cluster-specific, and are written by ``configure`` on each clone.

    Args:
        host: A connected SSH session to the template VM.
        config: The cluster configuration.
        source_dir: Local directory holding the Go service sources.

    Raises:
        VmDeployError: If any dependency cannot be installed or the app cannot
            be compiled.
    """
    _LOG.info("Baking golden image on %s: Apache, pinned Go, precompiled app", host.address)
    install_apache(host)
    install_go(host, config.build)

    host.sudo("groupadd -f vmdeploy-inventory")
    host.sudo(f"mkdir -p {config.inventory.remote_dir}")
    host.sudo(f"chgrp vmdeploy-inventory {config.inventory.remote_dir}")
    host.sudo(f"chmod 2770 {config.inventory.remote_dir}")

    build_service(host, source_dir, config.inventory.remote_dir)

    # Keep the exported appliance lean: drop the build cache and apt lists.
    host.run("rm -rf /tmp/vmdeploy-gocache", check=False)
    host.sudo("apt-get clean", check=False)
    _LOG.info(
        "Golden image baked on %s (source hash %s)", host.address, source_hash(source_dir)[:12]
    )


def render_unit(config: ClusterConfig) -> str:
    """Build the systemd unit for the inventory service.

    Returns:
        The unit file contents.

    Args:
        config: The cluster configuration.
    """
    inventory = config.inventory
    binary = f"{inventory.remote_dir}/{SERVICE_NAME}"
    return f"""# Managed by vmdeploy. Manual edits are overwritten on redeploy.
[Unit]
Description=Encrypted cluster inventory service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={binary} \\
    -addr :{inventory.service_port} \\
    -key {inventory.remote_dir}/{inventory.key_file.name} \\
    -store {inventory.remote_dir}/{inventory.datastore_name}
Restart=on-failure
RestartSec=5s

# The service reads one key and one datastore; nothing else needs to be
# reachable, so the sandbox is tightened to match.
DynamicUser=yes
SupplementaryGroups=vmdeploy-inventory
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths={inventory.remote_dir}
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6
RestrictNamespaces=yes
MemoryDenyWriteExecute=yes

[Install]
WantedBy=multi-user.target
"""


def deploy_service(host: RemoteHost, config: ClusterConfig, source_dir: Path) -> None:
    """Install, configure, and start the inventory service.

    Args:
        host: A connected SSH session to the jump station.
        config: The cluster configuration.
        source_dir: Local directory holding the Go service sources.

    Raises:
        VmDeployError: If any stage of deployment fails.
    """
    inventory = config.inventory

    # A dedicated group owns the key and datastore so the DynamicUser the unit
    # runs as can read them without the files being world readable.
    host.sudo("groupadd -f vmdeploy-inventory")
    host.sudo(f"mkdir -p {inventory.remote_dir}")
    # The service performs atomic writes by creating a temp file in this
    # directory and renaming it over the datastore, so the group needs write
    # and execute on the directory itself, not just on the datastore file. The
    # setgid bit makes every file created here inherit the group, so a future
    # DynamicUser instance (whose UID may differ) can still read what a prior
    # instance wrote.
    host.sudo(f"chgrp vmdeploy-inventory {inventory.remote_dir}")
    host.sudo(f"chmod 2770 {inventory.remote_dir}")

    # The golden image ships a precompiled binary; recompile only if the local
    # sources differ from what was baked in.
    ensure_service_binary(host, config, source_dir)

    key = load_or_create_key(inventory.key_file)
    remote_key = f"{inventory.remote_dir}/{inventory.key_file.name}"
    host.put_bytes(_encode_key(key), remote_key, mode=0o640)
    host.sudo(f"chgrp vmdeploy-inventory {remote_key}")
    host.sudo(f"chmod 0640 {remote_key}")

    host.put_bytes(render_unit(config).encode("utf-8"), _SERVICE_UNIT)
    host.sudo("systemctl daemon-reload")
    host.sudo(f"systemctl enable {SERVICE_NAME}.service")
    host.sudo(f"systemctl restart {SERVICE_NAME}.service")

    active = host.run(f"systemctl is-active {SERVICE_NAME}.service", check=False)
    if active.stdout.strip() != "active":
        journal = host.sudo(
            f"journalctl -u {SERVICE_NAME}.service --no-pager -n 40", check=False
        )
        raise VmDeployError(
            f"Inventory service failed to start on {host.address}: "
            f"{active.stdout.strip() or 'unknown'}\n{journal.stdout.strip()}"
        )
    _LOG.info("Inventory service active on %s:%d", host.address, inventory.service_port)


def _encode_key(key: bytes) -> bytes:
    """Encode a raw key as base64 ASCII for transport.

    Args:
        key: The raw key bytes.

    Returns:
        Base64 ASCII bytes with a trailing newline.
    """
    return base64.b64encode(key) + b"\n"


def build_records(
    config: ClusterConfig,
    addresses: dict[str, str],
    ipv6_addresses: dict[str, str] | None = None,
) -> tuple[InventoryRecord, ...]:
    """Build inventory records for every host with a resolved address.

    Args:
        config: The cluster configuration.
        addresses: Mapping of hostname to resolved IPv4 address.
        ipv6_addresses: Optional mapping of hostname to IPv6 address, as
            returned by :func:`enrich_with_ipv6`.

    Returns:
        Records in cluster order, jump station first.
    """
    timestamp = utc_timestamp()
    ipv6_map = ipv6_addresses or {}
    records: tuple[InventoryRecord, ...] = ()
    for host in config.all_hosts:
        address = addresses.get(host.hostname, "")
        records = upsert(
            records,
            InventoryRecord(
                hostname=host.hostname,
                role=host.role.value,
                ipv4=address,
                ipv6=ipv6_map.get(host.hostname, ""),
                status="Deployed" if address else "Unreachable",
                status_timestamp=timestamp,
                state="Active" if address else "Inactive",
                state_timestamp=timestamp,
            ),
        )
    return records


def seed_inventory(
    host: RemoteHost, config: ClusterConfig, records: tuple[InventoryRecord, ...]
) -> None:
    """Encrypt and write the inventory datastore on the jump station.

    Args:
        host: A connected SSH session to the jump station.
        config: The cluster configuration.
        records: The records to persist.

    Raises:
        VmDeployError: If the datastore cannot be written.
    """
    inventory = config.inventory
    key = load_or_create_key(inventory.key_file)
    blob = encrypt_records(records, key)

    remote_store = f"{inventory.remote_dir}/{inventory.datastore_name}"
    _LOG.info("Seeding %d inventory record(s) to %s", len(records), remote_store)
    host.put_bytes(blob, remote_store, mode=0o660)
    try:
        host.sudo(f"chgrp vmdeploy-inventory {remote_store}")
        host.sudo(f"chmod 0660 {remote_store}")
        host.sudo(f"systemctl restart {SERVICE_NAME}.service", check=False)
    except RemoteCommandError as exc:
        raise VmDeployError(
            f"Could not set ownership on {remote_store}: {exc}"
        ) from exc

    # The jump station's copy is destroyed on teardown; the local manifest is
    # the durable record, so it is written from the same records the service
    # was seeded with.
    write_local_manifest(config, records)


def write_local_manifest(config: ClusterConfig, records: tuple[InventoryRecord, ...]) -> None:
    """Write the canonical encrypted manifest to the local automation host.

    Args:
        config: The cluster configuration.
        records: The records to persist.

    Raises:
        InventoryError: If the manifest cannot be encrypted or written.
    """
    key = load_or_create_key(config.inventory.key_file)
    blob = encrypt_records(records, key)
    path = config.inventory.manifest_file
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
    except OSError as exc:
        raise InventoryError(f"Cannot write local manifest to {path}: {exc}") from exc
    _LOG.info("Wrote local manifest with %d record(s) to %s", len(records), path)


def read_local_manifest(config: ClusterConfig) -> tuple[InventoryRecord, ...]:
    """Read and decrypt the canonical local manifest.

    Args:
        config: The cluster configuration.

    Returns:
        The stored records, or an empty tuple if no manifest exists yet.

    Raises:
        InventoryError: If the manifest exists but cannot be read or decrypted.
    """
    path = config.inventory.manifest_file
    if not path.is_file():
        return ()
    key = load_key(config.inventory.key_file)
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise InventoryError(f"Cannot read local manifest at {path}: {exc}") from exc
    return decrypt_records(blob, key)


def mark_manifest_removed(config: ClusterConfig) -> tuple[InventoryRecord, ...]:
    """Update the local manifest to mark every cluster host as removed.

    Existing records are preserved and flipped to ``Removed``/``Inactive`` so
    their last-known addresses remain in the audit record. Hosts that are in
    the configuration but absent from the manifest are added as removed, so the
    manifest is complete regardless of how much of a prior deploy succeeded.

    Args:
        config: The cluster configuration.

    Returns:
        The updated records that were written to the manifest.

    Raises:
        InventoryError: If the manifest cannot be read or written.
    """
    timestamp = utc_timestamp()
    records = read_local_manifest(config)

    updated: tuple[InventoryRecord, ...] = ()
    seen: set[str] = set()
    for record in records:
        seen.add(record.hostname)
        updated = upsert(
            updated,
            InventoryRecord(
                hostname=record.hostname,
                role=record.role,
                ipv4=record.ipv4,
                ipv6=record.ipv6,
                status="Removed",
                status_timestamp=timestamp,
                state="Inactive",
                state_timestamp=timestamp,
            ),
        )

    for host in config.all_hosts:
        if host.hostname in seen:
            continue
        updated = upsert(
            updated,
            InventoryRecord(
                hostname=host.hostname,
                role=host.role.value,
                ipv4="",
                ipv6="",
                status="Removed",
                status_timestamp=timestamp,
                state="Inactive",
                state_timestamp=timestamp,
            ),
        )

    write_local_manifest(config, updated)
    return updated


def enrich_with_ipv6(config: ClusterConfig, addresses: dict[str, str]) -> dict[str, str]:
    """Collect each reachable host's primary IPv6 address.

    Args:
        config: The cluster configuration.
        addresses: Mapping of hostname to IPv4 address.

    Returns:
        Mapping of hostname to IPv6 address. Hosts that are unreachable or
        have no global IPv6 address are omitted rather than failing the run,
        because IPv6 is not required for any cluster function.
    """
    found: dict[str, str] = {}
    for host_config in config.all_hosts:
        address = addresses.get(host_config.hostname)
        if not address:
            continue
        try:
            with RemoteHost(address, config.ssh) as guest:
                result = guest.run(
                    "ip -6 addr show scope global | awk '/inet6/ {print $2}' "
                    "| cut -d/ -f1 | head -n 1",
                    check=False,
                )
                if result.stdout.strip():
                    found[host_config.hostname] = result.stdout.strip()
        except VmDeployError:
            _LOG.debug("Could not read IPv6 for %s", host_config.hostname)
    return found


def resolve_host(config: ClusterConfig, hostname: str) -> HostConfig:
    """Look up a host definition by hostname.

    Args:
        config: The cluster configuration.
        hostname: The hostname to look up.

    Returns:
        The matching host definition.

    Raises:
        VmDeployError: If the hostname is not part of the cluster.
    """
    for host in config.all_hosts:
        if host.hostname == hostname:
            return host
    raise VmDeployError(f"'{hostname}' is not a configured cluster host")
