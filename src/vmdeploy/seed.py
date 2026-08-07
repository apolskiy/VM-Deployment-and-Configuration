"""Build a NoCloud seed ISO so a guest configures itself on first boot.

The golden image deliberately ships with **no credentials at all** — image
sanitisation locks every account, so nothing in the appliance can be logged
into. Identity is supplied per guest at import time instead, by attaching a
small ISO that cloud-init reads on first boot (its NoCloud datasource).

That is what makes the image publishable. The appliance holds no shared
bootstrap account and no baked key, so nothing in it is secret; whoever imports
it injects *their own* public key through the seed and becomes the only account
that can log in. A published image is therefore useless to anyone who merely
downloads it, and immediately usable by anyone who deploys it properly.

cloud-init's NoCloud datasource looks for a filesystem labelled ``cidata``
holding ``user-data`` and ``meta-data`` at its root, so that is exactly what
this builds. The ISO is written with both Rock Ridge and Joliet extensions
because plain ISO 9660 would truncate those names to 8.3 uppercase and
cloud-init would not find them.

This module builds only the medium. Enabling cloud-init inside the image and
attaching the ISO to a guest are separate steps.
"""

from __future__ import annotations

import io
import logging
import re
import uuid
from pathlib import Path
from typing import Final

import pycdlib

from vmdeploy.exceptions import VmDeployError

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

# cloud-init identifies the NoCloud medium by this filesystem label. It is not
# configurable: a different label means the datasource is simply not found and
# the guest boots with no identity at all.
SEED_VOLUME_LABEL: Final[str] = "cidata"

# A single RFC 1123 hostname label: alphanumeric, inner hyphens allowed, 63 max.
_HOSTNAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)

# A portable POSIX account name, matching what useradd will accept.
_USERNAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# OpenSSH public key types this tooling accepts. Ed25519 is what `setup`
# generates; the others are allowed so an existing key can be reused.
_PUBLIC_KEY_TYPES: Final[tuple[str, ...]] = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)

# ISO 9660 identifiers must be uppercase 8.3 with a version suffix; the usable
# lowercase names are carried by the Rock Ridge and Joliet extensions.
_ISO_IDENTIFIERS: Final[dict[str, str]] = {
    "user-data": "/USERDATA.;1",
    "meta-data": "/METADATA.;1",
}


def _yaml_quote(value: str) -> str:
    """Quote a string as a YAML single-quoted scalar.

    Generating these two small documents by hand avoids taking on a YAML
    dependency for what is a fixed, known shape. Single-quoted style is the
    safest choice because it disables every escape sequence: the only character
    with meaning inside it is the single quote, which YAML escapes by doubling.

    Args:
        value: The string to quote.

    Returns:
        The value as a YAML single-quoted scalar, quotes included.
    """
    return "'" + value.replace("'", "''") + "'"


def _validate_hostname(hostname: str) -> None:
    """Reject a hostname cloud-init could not apply.

    Args:
        hostname: The guest hostname.

    Raises:
        VmDeployError: If the hostname is not a valid RFC 1123 label.
    """
    if not _HOSTNAME_PATTERN.match(hostname):
        raise VmDeployError(
            f"invalid hostname {hostname!r}: must be 1-63 characters of letters, digits, "
            "or inner hyphens, and must not start or end with a hyphen"
        )


def _validate_username(username: str) -> None:
    """Reject an account name useradd would refuse.

    Args:
        username: The operational account name.

    Raises:
        VmDeployError: If the name is not a portable POSIX account name.
    """
    if not _USERNAME_PATTERN.match(username):
        raise VmDeployError(
            f"invalid username {username!r}: must start with a lowercase letter or underscore "
            "and contain only lowercase letters, digits, underscores, or hyphens (32 max)"
        )


def _validate_public_key(public_key: str) -> str:
    """Check that a string is a single usable OpenSSH public key.

    A malformed key here would produce a guest that boots correctly and then
    refuses every login, which is expensive to diagnose against a headless VM.
    It is far cheaper to reject it before the ISO is written.

    Args:
        public_key: The candidate OpenSSH public key line.

    Returns:
        The key stripped of surrounding whitespace.

    Raises:
        VmDeployError: If the key is empty, spans multiple lines, or does not
            begin with a recognised key type.
    """
    candidate = public_key.strip()
    if not candidate:
        raise VmDeployError("the deployer public key is empty; nothing could log in to the guest")
    if "\n" in candidate or "\r" in candidate:
        raise VmDeployError(
            "the deployer public key spans multiple lines; supply exactly one OpenSSH public key"
        )
    if not candidate.startswith(_PUBLIC_KEY_TYPES):
        preview = candidate.split(None, 1)[0]
        raise VmDeployError(
            f"unrecognised public key type {preview!r}; expected one of {list(_PUBLIC_KEY_TYPES)}. "
            "Note this must be the .pub file, not the private key."
        )
    return candidate


def render_user_data(hostname: str, username: str, public_key: str) -> str:
    """Render the cloud-config document that creates the operational account.

    Only the named account is created: no ``default`` entry is included, so
    cloud-init does not also add the distribution's stock user. The account is
    key-only (``lock_passwd``) and password authentication is disabled outright,
    so the guest has no password login at all.

    Args:
        hostname: The guest hostname to apply.
        username: The operational account to create.
        public_key: The deployer's OpenSSH public key, authorised for that account.

    Returns:
        A ``#cloud-config`` YAML document.

    Raises:
        VmDeployError: If the hostname or public key is unusable.
    """
    _validate_hostname(hostname)
    _validate_username(username)
    key = _validate_public_key(public_key)
    return "\n".join(
        (
            "#cloud-config",
            f"hostname: {_yaml_quote(hostname)}",
            f"fqdn: {_yaml_quote(hostname)}",
            "preserve_hostname: false",
            # Debian-family systems map the hostname to 127.0.1.1. Leaving the
            # template's name there makes sudo emit resolution warnings and
            # breaks any service that resolves its own hostname at start-up.
            "manage_etc_hosts: true",
            # Each clone must present a distinct host key. Without this they
            # inherit the template's, and every guest looks like the same host.
            "ssh_deletekeys: true",
            "users:",
            f"  - name: {_yaml_quote(username)}",
            "    shell: /bin/bash",
            "    lock_passwd: true",
            "    sudo: 'ALL=(ALL) NOPASSWD:ALL'",
            "    ssh_authorized_keys:",
            f"      - {_yaml_quote(key)}",
            "ssh_pwauth: false",
            "disable_root: true",
            "",
        )
    )


def render_meta_data(hostname: str, instance_id: str) -> str:
    """Render the NoCloud meta-data document.

    Args:
        hostname: The guest hostname, also used as the local-hostname.
        instance_id: A value unique to this guest. cloud-init runs its per-instance
            modules only when this changes, so reusing one across guests would
            leave the second guest unconfigured.

    Returns:
        A YAML document with the instance id and local hostname.

    Raises:
        VmDeployError: If the hostname is unusable.
    """
    _validate_hostname(hostname)
    return "\n".join(
        (
            f"instance-id: {_yaml_quote(instance_id)}",
            f"local-hostname: {_yaml_quote(hostname)}",
            "",
        )
    )


def build_seed_iso(
    destination: Path,
    hostname: str,
    username: str,
    public_key: str,
    instance_id: str | None = None,
) -> Path:
    """Write a cloud-init NoCloud seed ISO for one guest.

    Args:
        destination: Path of the ISO to write. Any existing file is replaced and
            the parent directory is created.
        hostname: The hostname the guest should take.
        username: The operational account to create on the guest.
        public_key: The deployer's OpenSSH public key.
        instance_id: Optional explicit instance id. When omitted a unique one is
            generated, which is what makes each guest configure itself.

    Returns:
        The path written.

    Raises:
        VmDeployError: If the inputs are unusable or the ISO cannot be written.
    """
    resolved_id = instance_id or f"{hostname}-{uuid.uuid4().hex[:12]}"
    documents = {
        "user-data": render_user_data(hostname, username, public_key),
        "meta-data": render_meta_data(hostname, resolved_id),
    }

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)

    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident=SEED_VOLUME_LABEL)
    try:
        for name, text in documents.items():
            payload = text.encode("utf-8")
            iso.add_fp(
                io.BytesIO(payload),
                len(payload),
                _ISO_IDENTIFIERS[name],
                rr_name=name,
                joliet_path=f"/{name}",
            )
        iso.write(str(destination))
    except Exception as error:  # pycdlib raises its own exception hierarchy
        raise VmDeployError(f"could not write the seed ISO to {destination}: {error}") from error
    finally:
        iso.close()

    _LOG.info("Wrote cloud-init seed for %s to %s (instance-id %s)", hostname, destination,
              resolved_id)
    return destination
