"""First-run setup: generate credentials and harden the golden template.

An engineer clones this public repository with no secrets in it. ``setup`` makes
the environment usable and secure in one step:

1. Generate a fresh SSH keypair and the AES inventory key locally, into a
   gitignored directory. Nothing sensitive is ever committed.
2. Using the stock image's bootstrap account once, create a dedicated
   operational user on the template with the new key and passwordless sudo.
3. Verify the new user can log in and reach root **before** touching anything
   else, so a mistake can never lock the operator out.
4. Only then disable the bootstrap account (lock password, remove sudo, revoke
   its keys), so the deployed image ships without the well-known stock login.
5. Write the new identity into ``cluster.local.toml`` (gitignored) so every
   later command uses it automatically.

Run this against the template VM before ``vmdeploy template``, so the baked
golden image and all its clones carry only the hardened user.
"""

from __future__ import annotations

import dataclasses
import logging
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from vmdeploy.config import ClusterConfig, SSHConfig, local_overlay_path
from vmdeploy.exceptions import SSHConnectionError, VmDeployError
from vmdeploy.inventory import load_or_create_key
from vmdeploy.ssh_client import RemoteHost, wait_for_ssh
from vmdeploy.virtualbox import VBoxManage

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

DEFAULT_ADMIN_USER: Final[str] = "vmadmin"


def _shell_quote(value: str) -> str:
    """Quote a string as a single POSIX shell word.

    The stdlib ``shlex.quote`` targets the local platform (Windows here) while
    the remote shell is always POSIX, so quoting is done explicitly.

    Args:
        value: The string to quote.

    Returns:
        The value wrapped in single quotes with embedded quotes escaped.
    """
    return "'" + value.replace("'", "'\\''") + "'"


def generate_ssh_keypair(private_path: Path, comment: str) -> str:
    """Generate an unencrypted Ed25519 OpenSSH keypair on the local host.

    The private key is written with owner-only permissions and the public key
    alongside it with a ``.pub`` suffix. An existing keypair is left untouched
    so re-running setup does not invalidate a key already trusted by guests.

    Args:
        private_path: Destination path for the private key.
        comment: Comment appended to the public key, e.g. ``vmadmin@vmdeploy``.

    Returns:
        The public key line, including type, base64 body, and comment.

    Raises:
        VmDeployError: If the keypair cannot be written.
    """
    public_path = private_path.with_suffix(private_path.suffix + ".pub")
    if private_path.is_file() and public_path.is_file():
        _LOG.info("Reusing existing keypair at %s", private_path)
        return public_path.read_text(encoding="ascii").strip()

    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    public_line = f"{public_bytes.decode('ascii')} {comment}"

    try:
        private_path.parent.mkdir(parents=True, exist_ok=True)
        private_path.write_bytes(private_bytes)
        public_path.write_text(public_line + "\n", encoding="ascii")
    except OSError as exc:
        raise VmDeployError(f"Cannot write generated keypair to {private_path}: {exc}") from exc

    for path in (private_path, public_path):
        try:
            path.chmod(0o600)
        except OSError:
            _LOG.debug("Could not restrict permissions on %s", path)

    _LOG.info("Generated Ed25519 keypair at %s", private_path)
    return public_line


def create_admin_user(host: RemoteHost, username: str, public_key: str) -> None:
    """Create a sudo-capable user on the guest and install its public key.

    The operation is idempotent: an existing user is reconfigured rather than
    duplicated, so setup can be re-run safely.

    Args:
        host: A connected SSH session authenticated as the bootstrap account.
        username: The operational account to create.
        public_key: The OpenSSH public key line to authorise.

    Raises:
        VmDeployError: If the account cannot be created or configured.
    """
    _LOG.info("Creating operational user '%s' on %s", username, host.address)
    home = f"/home/{username}"
    ssh_dir = f"{home}/.ssh"
    authorized = f"{ssh_dir}/authorized_keys"

    if not host.run(f"id -u {username}", check=False).ok:
        host.sudo(f"useradd --create-home --shell /bin/bash {username}")
    host.sudo(f"usermod -aG sudo {username}")

    host.sudo(f"install -d -m 700 -o {username} -g {username} {ssh_dir}")
    # Write the key via a staged temp file so the public key never rides on a
    # command line where it could be truncated by shell quoting.
    host.put_bytes((public_key + "\n").encode("ascii"), authorized, mode=0o600)
    host.sudo(f"chown {username}:{username} {authorized}")
    host.sudo(f"chmod 600 {authorized}")

    sudoers = f"/etc/sudoers.d/90-{username}"
    host.put_bytes(
        f"{username} ALL=(ALL) NOPASSWD:ALL\n".encode("ascii"), sudoers, mode=0o440
    )
    # A malformed sudoers file locks out sudo entirely; validate before trusting.
    check = host.sudo(f"visudo -cf {sudoers}", check=False)
    if not check.ok:
        host.sudo(f"rm -f {sudoers}", check=False)
        raise VmDeployError(
            f"Generated sudoers file for {username} failed validation: "
            f"{check.stderr.strip() or check.stdout.strip()}"
        )


def verify_admin_access(address: str, ssh_config: SSHConfig) -> bool:
    """Confirm the new user can log in and reach root, without side effects.

    Args:
        address: The guest address to connect to.
        ssh_config: SSH settings for the new user and its key.

    Returns:
        True if login succeeds and passwordless sudo returns uid 0.
    """
    try:
        with RemoteHost(address, ssh_config) as host:
            result = host.sudo("id -u", check=False)
            return result.ok and result.stdout.strip() == "0"
    except (SSHConnectionError, VmDeployError) as exc:
        _LOG.warning("Verification of '%s' failed: %s", ssh_config.user, exc)
        return False


def disable_bootstrap_account(host: RemoteHost, username: str) -> None:
    """Lock the stock bootstrap account so it can no longer be used.

    The password is locked, the shell is set to nologin, sudo is revoked, and
    any authorised keys are removed, so neither the well-known key nor a
    password can reach the account.

    Args:
        host: A connected SSH session with sudo (as the new admin user).
        username: The bootstrap account to disable.

    Raises:
        VmDeployError: If the account cannot be disabled.
    """
    _LOG.info("Disabling bootstrap account '%s' on %s", username, host.address)
    host.sudo(f"usermod -L {username}", check=False)
    host.sudo(f"usermod -s /usr/sbin/nologin {username}", check=False)
    host.sudo(f"gpasswd -d {username} sudo", check=False)
    host.sudo(f"rm -f /etc/sudoers.d/90-{username}", check=False)
    host.sudo(f"rm -f /home/{username}/.ssh/authorized_keys", check=False)


def _emit_toml(data: Mapping[str, Any]) -> str:
    """Serialise a shallow table-of-scalars mapping to TOML.

    Handles exactly the shape the overlay uses, top-level tables whose values
    are strings, integers, or booleans, so no third-party TOML writer is
    needed. Top-level scalars are emitted before any table, as TOML requires.

    Args:
        data: A mapping of table name to a mapping of key to scalar value.

    Returns:
        The TOML text.
    """
    def render(value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, int):
            return str(value)
        return f'"{value}"'

    scalars = {key: val for key, val in data.items() if not isinstance(val, Mapping)}
    tables = {key: val for key, val in data.items() if isinstance(val, Mapping)}

    lines = [f"{key} = {render(val)}" for key, val in scalars.items()]
    for table, values in tables.items():
        lines.append(f"[{table}]")
        lines.extend(f"{key} = {render(val)}" for key, val in values.items())
        lines.append("")
    return "\n".join(lines).strip() + "\n"


_BUILD_TOOLING_PACKAGES: Final[tuple[str, ...]] = (
    "git",
    "gh",
    "docker.io",
    "docker-ce",
    "docker-ce-cli",
    "containerd",
    "containerd.io",
    "runc",
)

# Per-home credential and history artefacts a distributable image must not carry.
# authorized_keys is deliberately excluded: that is the operational access the
# cluster depends on, not a leaked secret.
_CREDENTIAL_GLOBS: Final[tuple[str, ...]] = (
    ".docker",
    ".git-credentials",
    ".gitconfig",
    ".config/gh",
    ".config/git",
    ".netrc",
    ".aws",
    ".bash_history",
    ".ssh/id_*",
    ".ssh/known_hosts",
    # Including authorized_keys is what makes the image publishable: it leaves
    # no account anyone can log in to, not even the builder's. Each guest is
    # given its own key at first boot from the seed ISO built by vmdeploy.seed,
    # and cloud-init installs it onto this account even though the account
    # already exists in the image.
    ".ssh/authorized_keys",
)


def sanitize_image(host: RemoteHost, operational_user: str) -> None:
    """Strip credentials and build tooling from the image before it is exported.

    A golden image is a distributable artefact, so it must carry no credentials
    at all and none of the build-host tooling that tends to cache them. This
    removes cached Docker/registry and git/gh credentials, shell history, and
    every SSH key, **including the operational account's own
    ``authorized_keys``**, from every account; locks every human account except
    the operational user; and purges git, docker, and gh.

    The exported image therefore has no login whatsoever. That is the point: it
    can be published without handing anyone access, because each guest receives
    its own key at first boot from a cloud-init seed (see :mod:`vmdeploy.seed`
    and :func:`arm_cloud_init`). The operational account is left in place, and
    cloud-init installs the deployer's key onto it even though it already
    exists.

    This is destructive and is intended to run only inside the snapshot-protected
    image build, which rolls the template box back afterward, so the working box
    keeps its keys, credentials, and accounts.

    Args:
        host: A connected SSH session with sudo (as the operational user).
        operational_user: The account left in place for cloud-init to key.

    Raises:
        VmDeployError: If sanitisation cannot be completed.
    """
    _LOG.info("Sanitising image on %s (removing credentials and build tooling)", host.address)

    # 1. Purge cached credentials and history from every home and from root.
    targets = " ".join(f'"$home"/{pattern}' for pattern in _CREDENTIAL_GLOBS)
    purge = (
        'for home in /root /home/*; do '
        '[ -d "$home" ] || continue; '
        f"rm -rf {targets}; "
        "done"
    )
    host.sudo(f"sh -c {_shell_quote(purge)}", check=False)

    # 2. Lock every non-system human account except the operational user, so the
    #    image ships with only the intended login. Snapshot rollback restores the
    #    disabled accounts on the working box.
    lock = (
        "getent passwd | awk -F: '$3>=1000 && $3<60000 {print $1}' | "
        f"while read -r account; do "
        f'  [ "$account" = "{operational_user}" ] && continue; '
        '  usermod -L "$account" 2>/dev/null || true; '
        '  usermod -s /usr/sbin/nologin "$account" 2>/dev/null || true; '
        '  gpasswd -d "$account" sudo 2>/dev/null || true; '
        '  rm -f "/etc/sudoers.d/90-$account"; '
        '  rm -f "/home/$account/.ssh/authorized_keys"; '
        "done"
    )
    host.sudo(f"sh -c {_shell_quote(lock)}", check=False)

    # 3. Purge build tooling and its data. The runtime cluster needs none of it.
    packages = " ".join(_BUILD_TOOLING_PACKAGES)
    host.sudo(
        f"DEBIAN_FRONTEND=noninteractive apt-get purge -y {packages} 2>/dev/null || true",
        check=False,
    )
    host.sudo("DEBIAN_FRONTEND=noninteractive apt-get autoremove -y 2>/dev/null || true",
              check=False)
    host.sudo("rm -rf /var/lib/docker /etc/docker /var/lib/containerd", check=False)
    host.sudo("groupdel docker 2>/dev/null || true", check=False)
    host.sudo("apt-get clean", check=False)

    # 4. Verify the build tooling is actually gone.
    leftover = host.run(
        "command -v git docker gh 2>/dev/null || true", check=False
    ).stdout.strip()
    if leftover:
        _LOG.warning("Build tooling still present after purge on %s: %s", host.address, leftover)

    _verify_no_authorized_keys(host)
    _LOG.info("Image sanitised on %s; it now carries no login of any kind", host.address)


def _verify_no_authorized_keys(host: RemoteHost) -> None:
    """Confirm no account in the image can still be logged in to.

    This is the check that makes publishing safe, so it fails the build rather
    than warning: a surviving ``authorized_keys`` would hand every downloader of
    the image a working login to every guest deployed from it.

    Args:
        host: A connected SSH session with sudo.

    Raises:
        VmDeployError: If any authorized_keys file survived.
    """
    survivors = host.sudo(
        "find /root /home -name authorized_keys 2>/dev/null || true", check=False
    ).stdout.split()
    if survivors:
        raise VmDeployError(
            f"authorized_keys survived sanitisation at {survivors}; the image would ship a "
            "working login and must not be published. Sanitisation did not complete."
        )


# Ubuntu's live installer switches cloud-init off after the first boot and
# records the opt-out in these two files. Both must go for a guest to configure
# itself from a seed, and the second matters more than it looks: it pins
# ``datasource_list`` to ``[None]``, so a guest with a perfectly good cidata ISO
# attached would never even probe for it and would boot with no identity at all.
_CLOUD_INIT_DISABLED_FLAG: Final[str] = "/etc/cloud/cloud-init.disabled"
_INSTALLER_DROPIN: Final[str] = "/etc/cloud/cloud.cfg.d/99-installer.cfg"

# Sorts after 99-installer.cfg, so NoCloud wins even if that file is ever
# reintroduced by a package update.
_DATASOURCE_DROPIN: Final[str] = "/etc/cloud/cloud.cfg.d/99-vmdeploy-nocloud.cfg"
_DATASOURCE_DROPIN_BODY: Final[str] = (
    "# Written by vmdeploy when baking the golden image.\n"
    "# The appliance ships with no credentials; each guest receives its identity\n"
    "# from a NoCloud seed ISO attached at import. Probing NoCloud first is what\n"
    "# makes that work; None remains as the fallback for a guest booted without\n"
    "# a seed, so it comes up rather than hanging.\n"
    "datasource_list: [ NoCloud, None ]\n"
)

# The image ships with cloud-init networking disabled by the Ubuntu installer,
# but that drop-in is removed by ``cloud-init clean`` (which runs the hooks in
# /etc/cloud/clean.d). Relying on it would leave cloud-init free to rewrite a
# working netplan on first boot, so the setting is restored here under a name
# clean does not touch.
_NETWORK_DROPIN: Final[str] = "/etc/cloud/cloud.cfg.d/99-vmdeploy-network.cfg"
_NETWORK_DROPIN_BODY: Final[str] = (
    "# Written by vmdeploy when baking the golden image.\n"
    "# The image already carries working netplan/DHCP configuration. cloud-init\n"
    "# must not generate its own alongside it, so its network handling is off.\n"
    "network: {config: disabled}\n"
)


def arm_cloud_init(host: RemoteHost) -> None:
    """Re-enable cloud-init in the image so each clone configures itself on boot.

    The golden image carries no credentials, so a guest cloned from it has no
    way in until something injects one. That something is a NoCloud seed ISO
    attached at import (see :mod:`vmdeploy.seed`), and this prepares the image
    to read it: it removes the installer's opt-out, removes the installer
    drop-in pinning the datasource to ``None``, pins ``NoCloud`` explicitly, and
    clears the recorded instance so the next boot is treated as a first boot.

    Network configuration is deliberately left alone: the image already ships
    ``00-subiquity-disable-cloudinit-networking.cfg``, so cloud-init will not
    rewrite the working netplan.

    This is intended to run inside the snapshot-protected image build, which
    rolls the template box back afterward.

    Args:
        host: A connected SSH session with sudo.

    Raises:
        VmDeployError: If the image cannot be armed, verified after the fact.
    """
    _LOG.info("Arming cloud-init for first-boot self-configuration on %s", host.address)

    host.sudo(f"rm -f {_CLOUD_INIT_DISABLED_FLAG} {_INSTALLER_DROPIN}")

    # Forget this boot's instance, logs, and machine id, so every clone treats
    # its first boot as a first boot and applies its own seed. Without this the
    # image carries a recorded instance and clones skip per-instance modules.
    #
    # This runs *before* the drop-ins are written, not after: clean executes the
    # hooks in /etc/cloud/clean.d, which delete the installer's drop-ins. Writing
    # ours first would mean cleaning them away again.
    host.sudo("cloud-init clean --logs --machine-id", check=False)

    for remote_path, body in (
        (_DATASOURCE_DROPIN, _DATASOURCE_DROPIN_BODY),
        (_NETWORK_DROPIN, _NETWORK_DROPIN_BODY),
    ):
        staged = f"/tmp/{Path(remote_path).name}"
        host.put_bytes(body.encode("utf-8"), staged)
        host.sudo(f"install -m 0644 -o root -g root {staged} {remote_path}")
        host.run(f"rm -f {staged}", check=False)

    _verify_cloud_init_armed(host)
    _LOG.info("cloud-init armed on %s; clones will self-configure from a seed", host.address)


def _verify_cloud_init_armed(host: RemoteHost) -> None:
    """Confirm the image will actually read a seed before it is exported.

    A silent failure here produces an appliance that boots normally and is
    unreachable, with nothing in the image to explain why, so it is checked
    while a session is still open rather than discovered against a headless VM.

    Args:
        host: A connected SSH session with sudo.

    Raises:
        VmDeployError: If an installer opt-out survived or NoCloud is not
            selectable.
    """
    leftovers = host.run(
        f"test -e {_CLOUD_INIT_DISABLED_FLAG} && echo {_CLOUD_INIT_DISABLED_FLAG}; "
        f"test -e {_INSTALLER_DROPIN} && echo {_INSTALLER_DROPIN}; true",
        check=False,
    ).stdout.strip()
    if leftovers:
        raise VmDeployError(
            "cloud-init is still disabled in the image; these installer opt-outs survived: "
            f"{leftovers.split()}. Clones would boot with no identity and no way in."
        )

    # Every privileged read here ends in "|| true". RemoteHost.sudo cannot tell a
    # command that legitimately exits non-zero from sudo itself being refused, so
    # a missing file would surface as a confusing password error instead of the
    # diagnosis below.
    datasource = host.sudo(f"cat {_DATASOURCE_DROPIN} 2>/dev/null || true", check=False).stdout
    if "NoCloud" not in datasource:
        raise VmDeployError(
            f"the NoCloud datasource drop-in at {_DATASOURCE_DROPIN} is missing or unreadable; "
            "clones would not probe for their seed ISO and would boot with no identity."
        )

    network = host.sudo(f"cat {_NETWORK_DROPIN} 2>/dev/null || true", check=False).stdout
    if "disabled" not in network:
        raise VmDeployError(
            f"the network drop-in at {_NETWORK_DROPIN} is missing or unreadable; cloud-init "
            "would regenerate netplan on first boot and could break guest networking."
        )


def enable_account(host: RemoteHost, username: str, public_key: str) -> None:
    """Re-enable a previously disabled account and restore its access.

    This is the inverse of :func:`disable_bootstrap_account`: it unlocks the
    password, restores an interactive shell, re-adds the account to the sudo
    group with a passwordless rule, and reinstates the supplied public key. Use
    it to bring the bootstrap account back on the *template box* after the
    hardened image has been exported. The exported OVA and its clones are
    unaffected and stay hardened.

    Args:
        host: A connected SSH session with sudo (as the operational user).
        username: The account to re-enable.
        public_key: The OpenSSH public key line to reauthorise for the account.

    Raises:
        VmDeployError: If the account cannot be re-enabled.
    """
    _LOG.info("Re-enabling account '%s' on %s", username, host.address)
    if not host.run(f"id -u {username}", check=False).ok:
        raise VmDeployError(f"Account '{username}' does not exist on {host.address}")

    host.sudo(f"usermod -U {username}", check=False)
    host.sudo(f"usermod -s /bin/bash {username}")
    host.sudo(f"usermod -aG sudo {username}")

    ssh_dir = f"/home/{username}/.ssh"
    authorized = f"{ssh_dir}/authorized_keys"
    host.sudo(f"install -d -m 700 -o {username} -g {username} {ssh_dir}")
    host.put_bytes((public_key.strip() + "\n").encode("ascii"), authorized, mode=0o600)
    host.sudo(f"chown {username}:{username} {authorized}")
    host.sudo(f"chmod 600 {authorized}")

    sudoers = f"/etc/sudoers.d/90-{username}"
    host.put_bytes(
        f"{username} ALL=(ALL) NOPASSWD:ALL\n".encode("ascii"), sudoers, mode=0o440
    )
    check = host.sudo(f"visudo -cf {sudoers}", check=False)
    if not check.ok:
        host.sudo(f"rm -f {sudoers}", check=False)
        raise VmDeployError(f"Restored sudoers for {username} failed validation")


def restore_bootstrap(
    config: ClusterConfig, *, username: str, public_key: str, leave_running: bool
) -> None:
    """Re-enable a disabled account on the template box after image export.

    Args:
        config: The loaded configuration (ssh section is the operational user).
        username: The account to re-enable, e.g. the original bootstrap login.
        public_key: The OpenSSH public key line to reauthorise.
        leave_running: Whether to leave the template powered on afterward.

    Raises:
        VmDeployError: If the template is unavailable or the account cannot be
            re-enabled.
        ProvisioningTimeoutError: If the template never becomes reachable.
    """
    vbox = VBoxManage(config.virtualbox.vboxmanage)
    template = config.virtualbox.template_vm
    if not vbox.exists(template):
        raise VmDeployError(f"Template machine '{template}' is not registered")

    vbox.start_headless(template)
    address = vbox.wait_for_guest_ip(template, template, config.virtualbox.boot_timeout_seconds)
    wait_for_ssh(address, config.ssh, config.virtualbox.boot_timeout_seconds)

    with RemoteHost(address, config.ssh) as host:
        enable_account(host, username, public_key)

    if not leave_running:
        vbox.power_off(template)
    _LOG.info(
        "Account '%s' re-enabled on template %s (%s). The exported golden image is "
        "unaffected and remains hardened.",
        username,
        template,
        "left running" if leave_running else "powered off",
    )


def write_local_overlay(base_config_path: Path, username: str, key_path: Path) -> Path:
    """Update the gitignored local overlay to point at the new identity.

    Any existing overlay is preserved and only its ``[ssh]`` user and key path
    are updated, so operator-specific overrides (inventory paths, website source,
    a custom VirtualBox path) survive a re-run of setup.

    Args:
        base_config_path: Path to the committed base configuration.
        username: The operational account name.
        key_path: Local path to the operational private key.

    Returns:
        The overlay path that was written.

    Raises:
        VmDeployError: If the overlay cannot be read or written.
    """
    overlay = local_overlay_path(base_config_path)

    existing: dict[str, Any] = {}
    if overlay.is_file():
        try:
            existing = tomllib.loads(overlay.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise VmDeployError(f"Cannot read existing overlay {overlay}: {exc}") from exc

    ssh_table = dict(existing.get("ssh", {}))
    ssh_table["user"] = username
    ssh_table["key_path"] = key_path.as_posix()
    existing["ssh"] = ssh_table

    header = (
        "# Local, machine-specific overrides. GITIGNORED, never committed.\n"
        "# [ssh] was written by `vmdeploy setup`; other sections are preserved.\n\n"
    )
    try:
        overlay.write_text(header + _emit_toml(existing), encoding="utf-8")
    except OSError as exc:
        raise VmDeployError(f"Cannot write local overlay {overlay}: {exc}") from exc
    _LOG.info("Updated local overlay %s (user '%s')", overlay, username)
    return overlay


def run_keys_only_setup(
    config: ClusterConfig,
    base_config_path: Path,
    *,
    new_user: str,
    new_key_path: Path,
) -> None:
    """Generate local credentials only, without touching any template machine.

    This is the setup path for someone who **pulls** the published golden image
    rather than baking their own. They have no template VM to harden, but they
    still need the two pieces of local key material the cluster is built from:
    the SSH keypair whose public half is written into every guest's cloud-init
    seed, and the AES key the inventory service is sealed with.

    Nothing is contacted over the network, so this works before any VM exists.

    Args:
        config: The loaded configuration, providing the inventory key location.
        base_config_path: Path to the committed base configuration.
        new_user: The operational account guests will be given.
        new_key_path: Local path for the generated private key.

    Raises:
        VmDeployError: If the key material cannot be written.
    """
    generate_ssh_keypair(new_key_path, comment=f"{new_user}@vmdeploy")
    load_or_create_key(config.inventory.key_file)
    write_local_overlay(base_config_path, new_user, new_key_path)
    _LOG.info(
        "Keys ready: '%s' with key %s. No template was touched. Guests built from the "
        "published image will accept this key and no other. Next: pull the image "
        "(scripts/pull-image.ps1), then 'vmdeploy provision'.",
        new_user,
        new_key_path,
    )


def run_setup(
    config: ClusterConfig,
    base_config_path: Path,
    *,
    new_user: str,
    new_key_path: Path,
) -> None:
    """Generate credentials, create the hardened user, and disable the stock one.

    The current configuration's SSH settings are the bootstrap identity: setup
    connects with them once to create the new user, then rewrites the local
    overlay so all later commands use the new identity.

    Args:
        config: The loaded configuration (its ssh section is the bootstrap identity).
        base_config_path: Path to the committed base configuration.
        new_user: The operational account to create.
        new_key_path: Local path for the new private key.

    Raises:
        VmDeployError: If the template is unavailable or hardening cannot be
            completed and verified.
        ProvisioningTimeoutError: If the template never becomes reachable.
    """
    if new_user == config.ssh.user:
        raise VmDeployError(
            f"new_user '{new_user}' must differ from the bootstrap user "
            f"'{config.ssh.user}'"
        )

    # 1. Local credential material.
    public_key = generate_ssh_keypair(new_key_path, comment=f"{new_user}@vmdeploy")
    load_or_create_key(config.inventory.key_file)

    # 2. Reach the template with the bootstrap identity.
    vbox = VBoxManage(config.virtualbox.vboxmanage)
    template = config.virtualbox.template_vm
    if not vbox.exists(template):
        raise VmDeployError(f"Template machine '{template}' is not registered")
    vbox.start_headless(template)
    address = vbox.wait_for_guest_ip(template, template, config.virtualbox.boot_timeout_seconds)
    wait_for_ssh(address, config.ssh, config.virtualbox.boot_timeout_seconds)

    # 3. Create the new user with the bootstrap account.
    with RemoteHost(address, config.ssh) as bootstrap:
        create_admin_user(bootstrap, new_user, public_key)

    # 4. Verify the new user works BEFORE disabling anything.
    new_ssh = dataclasses.replace(config.ssh, user=new_user, key_path=new_key_path)
    if not verify_admin_access(address, new_ssh):
        raise VmDeployError(
            f"New user '{new_user}' could not log in with sudo; leaving the bootstrap "
            f"account '{config.ssh.user}' enabled. No changes to access were made."
        )
    _LOG.info("Verified '%s' has working key-based login and sudo", new_user)

    # 5. Now it is safe to disable the stock account, acting as the new user.
    with RemoteHost(address, new_ssh) as admin:
        disable_bootstrap_account(admin, config.ssh.user)

    # 6. Persist the new identity for every later command.
    write_local_overlay(base_config_path, new_user, new_key_path)

    _LOG.info(
        "Setup complete: '%s' is the operational user and '%s' is disabled on the "
        "template. Run 'vmdeploy template' to bake the hardened golden image.",
        new_user,
        config.ssh.user,
    )
