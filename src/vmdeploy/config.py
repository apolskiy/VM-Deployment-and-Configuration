"""Typed, validated cluster configuration loaded from TOML.

Configuration is deliberately explicit rather than inferred: provisioning
destroys and recreates virtual machines, so every host name, port, and file
path is stated in one auditable document. Loading validates types and value
ranges up front so a malformed field fails before any VM is touched.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

from vmdeploy.exceptions import ConfigurationError

_MIN_PORT: Final[int] = 1
_MAX_PORT: Final[int] = 65535
_MIN_MEMORY_MB: Final[int] = 512


class HostRole(Enum):
    """The functional role a cluster host plays.

    Attributes:
        JUMP: The jump station running Apache as a load balancer and hosting
            the Go inventory service.
        BACKEND: A web server serving the static site behind the balancer.
    """

    JUMP = "jump"
    BACKEND = "backend"


@dataclass(frozen=True, slots=True)
class SSHConfig:
    """Credentials and timeouts used for every SSH connection.

    Attributes:
        user: The remote login account, shared by all cluster hosts because
            they are cloned from one golden template.
        key_path: Path to the OpenSSH-format private key used for auth.
        connect_timeout_seconds: TCP and auth timeout for a single attempt.
        command_timeout_seconds: Deadline for a single remote command.
    """

    user: str
    key_path: Path
    connect_timeout_seconds: int
    command_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class VirtualBoxConfig:
    """Hypervisor paths and guest sizing for provisioned virtual machines.

    Attributes:
        vboxmanage: Absolute path to the ``VBoxManage`` executable.
        template_vm: Name of the registered VM exported as the golden image.
        template_ova: Path the golden OVA is written to and imported from.
        template_image_ref: OCI reference the golden OVA is published to and
            pulled from. Registries come and go, so this lives in configuration
            rather than in documentation: moving the image to another registry,
            or republishing it built on a newer Ubuntu, is a one-line change
            here and every script follows it.
        memory_mb: RAM assigned to each provisioned guest.
        cpus: Virtual CPU count assigned to each provisioned guest.
        boot_timeout_seconds: How long to wait for a guest to accept SSH.
    """

    vboxmanage: Path
    template_vm: str
    template_ova: Path
    template_image_ref: str
    memory_mb: int
    cpus: int
    boot_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class HostConfig:
    """A single virtual machine in the cluster.

    Attributes:
        vm_name: The VirtualBox machine name used by ``VBoxManage``.
        hostname: The in-guest hostname set after first boot.
        role: Whether this host balances traffic or serves it.
        http_port: The TCP port Apache listens on inside the guest.
    """

    vm_name: str
    hostname: str
    role: HostRole
    http_port: int


@dataclass(frozen=True, slots=True)
class InventoryConfig:
    """Settings for the encrypted host registry and its Go service.

    Attributes:
        service_port: TCP port the Go inventory service listens on.
        remote_dir: Directory on the jump station holding the service,
            its encrypted datastore, and its HTML template.
        key_file: Local path to the 32-byte AES-256-GCM key. The same key is
            uploaded to the jump station so the Go service can decrypt.
        datastore_name: File name of the encrypted registry inside remote_dir.
        manifest_file: Local path to the canonical encrypted manifest. Both
            deploy and teardown rewrite it, so a durable, auditable record of
            cluster state survives even after the jump station that serves the
            live copy is destroyed. Encrypted with the same key as the service.
    """

    service_port: int
    remote_dir: str
    key_file: Path
    datastore_name: str
    manifest_file: Path


@dataclass(frozen=True, slots=True)
class WebsiteConfig:
    """Source of the static site published to the backend web servers.

    Attributes:
        source_url: The live site URL, used to validate served content.
        archive_url: Tarball of the site repository's default branch.
        document_root: Apache document root inside each backend guest.
    """

    source_url: str
    archive_url: str
    document_root: str


@dataclass(frozen=True, slots=True)
class BuildConfig:
    """Toolchain pinning for the golden image and the inventory service build.

    Attributes:
        go_version: The exact Go release baked into the golden image, without
            the ``go`` prefix, e.g. ``1.26.5``. Pinning a version rather than
            using the distribution package keeps rebuilds reproducible.
        go_sha256: The expected SHA-256 of the linux-amd64 Go archive. The
            template build verifies the download against this before extracting,
            so a corrupted or substituted archive fails closed.
    """

    go_version: str
    go_sha256: str


@dataclass(frozen=True, slots=True)
class ClusterConfig:
    """The complete, validated description of the target cluster.

    Attributes:
        ssh: Shared SSH credentials and timeouts.
        virtualbox: Hypervisor paths and guest sizing.
        jump: The jump station host.
        backends: The backend web servers, in balancer member order.
        inventory: Encrypted registry and Go service settings.
        website: Static site source and document root.
        build: Toolchain pinning for the golden image.
    """

    ssh: SSHConfig
    virtualbox: VirtualBoxConfig
    jump: HostConfig
    backends: tuple[HostConfig, ...]
    inventory: InventoryConfig
    website: WebsiteConfig
    build: BuildConfig

    @property
    def all_hosts(self) -> tuple[HostConfig, ...]:
        """Return every host in the cluster, jump station first.

        Returns:
            A tuple containing the jump station followed by each backend.
        """
        return (self.jump, *self.backends)


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Extract a required table from a parsed TOML document.

    Args:
        data: The parsed TOML mapping.
        name: The table name to extract.

    Returns:
        The requested table as a mapping.

    Raises:
        ConfigurationError: If the table is absent or is not a table.
    """
    value = data.get(name)
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"Missing or malformed [{name}] table in cluster configuration")
    return value


def _require_str(section: Mapping[str, Any], key: str, context: str) -> str:
    """Read a required non-empty string field.

    Args:
        section: The table to read from.
        key: The field name.
        context: Human-readable location used in error messages.

    Returns:
        The field value with surrounding whitespace stripped.

    Raises:
        ConfigurationError: If the field is missing, not a string, or blank.
    """
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"[{context}] requires a non-empty string field '{key}'")
    return value.strip()


def _optional_str(section: Mapping[str, Any], key: str, default: str = "") -> str:
    """Read an optional string field, falling back to a default.

    Args:
        section: The table to read from.
        key: The field name.
        default: Value to use when the field is absent or blank.

    Returns:
        The field value with surrounding whitespace stripped, or the default.
    """
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        return default
    return value.strip()


def _require_int(section: Mapping[str, Any], key: str, context: str, minimum: int) -> int:
    """Read a required integer field constrained by a lower bound.

    Booleans are rejected explicitly because ``bool`` is a subclass of ``int``
    in Python and would otherwise pass silently as 0 or 1.

    Args:
        section: The table to read from.
        key: The field name.
        context: Human-readable location used in error messages.
        minimum: The smallest permitted value, inclusive.

    Returns:
        The validated integer value.

    Raises:
        ConfigurationError: If the field is missing, not an integer, or below
            the permitted minimum.
    """
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"[{context}] requires an integer field '{key}'")
    if value < minimum:
        raise ConfigurationError(f"[{context}] field '{key}' must be >= {minimum}, got {value}")
    return value


def _require_port(section: Mapping[str, Any], key: str, context: str) -> int:
    """Read a required TCP port field.

    Args:
        section: The table to read from.
        key: The field name.
        context: Human-readable location used in error messages.

    Returns:
        The validated port number.

    Raises:
        ConfigurationError: If the value is not a port in the range 1-65535.
    """
    port = _require_int(section, key, context, _MIN_PORT)
    if port > _MAX_PORT:
        raise ConfigurationError(f"[{context}] field '{key}' must be <= {_MAX_PORT}, got {port}")
    return port


def _require_path(section: Mapping[str, Any], key: str, context: str) -> Path:
    """Read a required filesystem path field, expanding variables and ``~``.

    Environment variables (``%USERPROFILE%``, ``$HOME``, ``%VBOX_MSI_INSTALL_PATH%``)
    and a leading ``~`` are expanded, so one configuration file is portable
    across machines and users without hard-coding anyone's home directory. The
    path is deliberately not checked for existence: golden OVA templates and
    inventory keys are created by this tool, so they are legitimately absent on
    a first run.

    Args:
        section: The table to read from.
        key: The field name.
        context: Human-readable location used in error messages.

    Returns:
        The expanded path.

    Raises:
        ConfigurationError: If the field is missing or not a string, or if a
            referenced environment variable is undefined (which would otherwise
            leave an unexpanded ``%VAR%`` fragment in the path).
    """
    raw_value = _require_str(section, key, context)
    expanded = os.path.expanduser(os.path.expandvars(raw_value))
    if "%" in expanded or "$" in expanded:
        raise ConfigurationError(
            f"[{context}] field '{key}' still contains an unexpanded environment "
            f"variable after expansion: {expanded!r}. Check the variable is set."
        )
    return Path(expanded)


def _parse_backends(raw: Any) -> tuple[HostConfig, ...]:
    """Build backend host definitions from the TOML array of tables.

    Args:
        raw: The value of the ``backend`` key in the parsed document.

    Returns:
        Backend hosts in declaration order, which is also balancer member
        order.

    Raises:
        ConfigurationError: If fewer than two backends are declared or any
            entry is malformed. Two are required because the load balancing
            test suite asserts distribution across a minimum of two members.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ConfigurationError("Cluster configuration requires an array of [[backend]] tables")
    if len(raw) < 2:
        raise ConfigurationError(
            f"At least two [[backend]] tables are required to validate load "
            f"balancing distribution, found {len(raw)}"
        )

    backends: list[HostConfig] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            raise ConfigurationError(f"[[backend]] entry {index} is not a table")
        context = f"backend[{index}]"
        backends.append(
            HostConfig(
                vm_name=_require_str(entry, "vm_name", context),
                hostname=_require_str(entry, "hostname", context),
                role=HostRole.BACKEND,
                http_port=_require_port(entry, "http_port", context),
            )
        )

    names = [backend.vm_name for backend in backends]
    if len(set(names)) != len(names):
        raise ConfigurationError(f"Duplicate backend vm_name values: {names}")
    return tuple(backends)


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge an overlay mapping over a base dictionary.

    Nested tables are merged key by key; scalar and array values in the overlay
    replace those in the base. The base is not mutated.

    Args:
        base: The base document.
        overlay: Values that take precedence over the base.

    Returns:
        A new merged dictionary.
    """
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _load_toml(path: Path) -> dict[str, Any]:
    """Read and parse a TOML file.

    Args:
        path: The file to read.

    Returns:
        The parsed document.

    Raises:
        ConfigurationError: If the file cannot be read or is not valid TOML.
    """
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"Cannot read cluster configuration at {path}: {exc}") from exc
    try:
        return tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigurationError(f"Invalid TOML in {path}: {exc}") from exc


def local_overlay_path(path: Path) -> Path:
    """Return the local-overlay path that sits beside a base configuration.

    Args:
        path: The base configuration path, e.g. ``config/cluster.toml``.

    Returns:
        The sibling overlay path, e.g. ``config/cluster.local.toml``.
    """
    return path.with_name(f"{path.stem}.local{path.suffix}")


def load_cluster_config(path: Path) -> ClusterConfig:
    """Load and validate a cluster configuration document.

    The committed base file is safe to publish: it carries no personal account
    name and no secrets. Machine- and operator-specific values live in a
    gitignored ``<name>.local.toml`` overlay beside it, deep-merged over the
    base, and the SSH user can additionally be overridden by the
    ``VMDEPLOY_SSH_USER`` environment variable. This keeps a real login name out
    of the public repository.

    Args:
        path: Path to the base TOML configuration file.

    Returns:
        The fully validated cluster configuration.

    Raises:
        ConfigurationError: If a file is missing, is not valid TOML, or fails
            any structural or value validation.
    """
    document: dict[str, Any] = _load_toml(path)

    overlay_path = local_overlay_path(path)
    if overlay_path.is_file():
        document = _deep_merge(document, _load_toml(overlay_path))

    env_user = os.environ.get("VMDEPLOY_SSH_USER", "").strip()
    if env_user:
        ssh_section = document.get("ssh")
        if isinstance(ssh_section, dict):
            ssh_section["user"] = env_user

    ssh_table = _section(document, "ssh")
    vbox_table = _section(document, "virtualbox")
    jump_table = _section(document, "jump")
    inventory_table = _section(document, "inventory")
    website_table = _section(document, "website")
    build_table = _section(document, "build")

    config = ClusterConfig(
        ssh=SSHConfig(
            user=_require_str(ssh_table, "user", "ssh"),
            key_path=_require_path(ssh_table, "key_path", "ssh"),
            connect_timeout_seconds=_require_int(ssh_table, "connect_timeout_seconds", "ssh", 1),
            command_timeout_seconds=_require_int(ssh_table, "command_timeout_seconds", "ssh", 1),
        ),
        virtualbox=VirtualBoxConfig(
            vboxmanage=_require_path(vbox_table, "vboxmanage", "virtualbox"),
            template_vm=_require_str(vbox_table, "template_vm", "virtualbox"),
            template_ova=_require_path(vbox_table, "template_ova", "virtualbox"),
            # Optional: only the publish/pull scripts read it, and a cluster
            # built from a locally baked OVA never needs it. Requiring it would
            # break every existing configuration for a field most runs ignore.
            template_image_ref=_optional_str(vbox_table, "template_image_ref"),
            memory_mb=_require_int(vbox_table, "memory_mb", "virtualbox", _MIN_MEMORY_MB),
            cpus=_require_int(vbox_table, "cpus", "virtualbox", 1),
            boot_timeout_seconds=_require_int(vbox_table, "boot_timeout_seconds", "virtualbox", 30),
        ),
        jump=HostConfig(
            vm_name=_require_str(jump_table, "vm_name", "jump"),
            hostname=_require_str(jump_table, "hostname", "jump"),
            role=HostRole.JUMP,
            http_port=_require_port(jump_table, "http_port", "jump"),
        ),
        backends=_parse_backends(document.get("backend")),
        inventory=InventoryConfig(
            service_port=_require_port(inventory_table, "service_port", "inventory"),
            remote_dir=_require_str(inventory_table, "remote_dir", "inventory"),
            key_file=_require_path(inventory_table, "key_file", "inventory"),
            datastore_name=_require_str(inventory_table, "datastore_name", "inventory"),
            manifest_file=_require_path(inventory_table, "manifest_file", "inventory"),
        ),
        website=WebsiteConfig(
            source_url=_require_str(website_table, "source_url", "website"),
            archive_url=_require_str(website_table, "archive_url", "website"),
            document_root=_require_str(website_table, "document_root", "website"),
        ),
        build=BuildConfig(
            go_version=_require_str(build_table, "go_version", "build"),
            go_sha256=_require_str(build_table, "go_sha256", "build"),
        ),
    )

    _validate_cross_references(config)
    return config


def _validate_cross_references(config: ClusterConfig) -> None:
    """Check invariants that span multiple configuration sections.

    Args:
        config: The assembled configuration to check.

    Raises:
        ConfigurationError: If VM names collide across roles, or if the
            inventory service port collides with the jump station's Apache
            port, which would leave one of the two services unable to bind.
    """
    vm_names = [host.vm_name for host in config.all_hosts]
    if len(set(vm_names)) != len(vm_names):
        raise ConfigurationError(f"Cluster vm_name values must be unique, got {vm_names}")

    if config.virtualbox.template_vm in vm_names:
        raise ConfigurationError(
            f"template_vm '{config.virtualbox.template_vm}' collides with a cluster VM name; "
            "the golden template must remain a separate machine"
        )

    if config.inventory.service_port == config.jump.http_port:
        raise ConfigurationError(
            f"inventory.service_port ({config.inventory.service_port}) collides with "
            f"jump.http_port ({config.jump.http_port})"
        )
