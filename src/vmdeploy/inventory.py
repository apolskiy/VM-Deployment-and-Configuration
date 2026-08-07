"""Client for the AES-256-GCM encrypted host registry.

The DynoNode reference implementation stored its cluster registry as
base64-encoded CSV and described it as encrypted. Base64 is an encoding, not a
cipher: anyone who can read the file can read the registry. This module
replaces it with authenticated encryption.

Wire format, chosen so the Go service and this client interoperate byte for
byte::

    base64( nonce[12] || ciphertext || tag[16] )

That layout is exactly what Go's ``gcm.Seal(nonce, nonce, plaintext, nil)``
emits and what Python's ``AESGCM.encrypt`` produces once the nonce is
prepended, so neither side needs a framing header. The plaintext is a UTF-8
JSON array of host records.

Because GCM is an authenticated mode, any corruption, truncation, or tampering
fails the tag check and raises :class:`DecryptionError` rather than returning
plausible-looking garbage.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vmdeploy.exceptions import DecryptionError, InventoryError

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

KEY_BYTES: Final[int] = 32
NONCE_BYTES: Final[int] = 12
TAG_BYTES: Final[int] = 16

_REQUIRED_FIELDS: Final[frozenset[str]] = frozenset(
    {"hostname", "role", "ipv4", "ipv6", "status", "status_timestamp", "state", "state_timestamp"}
)


def utc_timestamp() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Returns:
        The current time formatted as ``YYYY-MM-DDTHH:MM:SSZ``.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class InventoryRecord:
    """One host in the encrypted cluster registry.

    Attributes:
        hostname: The guest's hostname.
        role: Functional role, either ``jump`` or ``backend``.
        ipv4: Primary IPv4 address.
        ipv6: Primary IPv6 address, or an empty string if none.
        status: Deployment status, such as ``Deployed`` or ``Removed``.
        status_timestamp: When status last changed, ISO-8601 UTC.
        state: Runtime state, such as ``Active`` or ``Inactive``.
        state_timestamp: When state last changed, ISO-8601 UTC.
    """

    hostname: str
    role: str
    ipv4: str
    ipv6: str
    status: str
    status_timestamp: str
    state: str
    state_timestamp: str

    def to_dict(self) -> dict[str, str]:
        """Serialise the record to a plain dictionary.

        Returns:
            The record's fields as a JSON-ready mapping.
        """
        return {key: str(value) for key, value in asdict(self).items()}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> InventoryRecord:
        """Build a record from a decoded JSON object.

        Args:
            payload: A decoded JSON object.

        Returns:
            The parsed record.

        Raises:
            InventoryError: If any required field is missing.
        """
        missing = _REQUIRED_FIELDS - payload.keys()
        if missing:
            raise InventoryError(
                f"Inventory record is missing required field(s): {sorted(missing)}"
            )
        return cls(
            hostname=str(payload["hostname"]),
            role=str(payload["role"]),
            ipv4=str(payload["ipv4"]),
            ipv6=str(payload["ipv6"]),
            status=str(payload["status"]),
            status_timestamp=str(payload["status_timestamp"]),
            state=str(payload["state"]),
            state_timestamp=str(payload["state_timestamp"]),
        )


def generate_key() -> bytes:
    """Generate a fresh AES-256 key.

    Returns:
        Thirty-two cryptographically random bytes.
    """
    return secrets.token_bytes(KEY_BYTES)


def write_key_file(key: bytes, path: Path) -> None:
    """Persist a key as base64 text with owner-only permissions.

    Args:
        key: The raw key bytes.
        path: Destination path for the key file.

    Raises:
        InventoryError: If the key is the wrong length or cannot be written.
    """
    if len(key) != KEY_BYTES:
        raise InventoryError(f"AES-256 key must be {KEY_BYTES} bytes, got {len(key)}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(base64.b64encode(key).decode("ascii"), encoding="ascii")
    except OSError as exc:
        raise InventoryError(f"Cannot write inventory key to {path}: {exc}") from exc

    # Best effort on Windows, where POSIX mode bits are only partly honoured.
    try:
        path.chmod(0o600)
    except OSError:
        _LOG.debug("Could not restrict permissions on %s", path)


def load_key(path: Path) -> bytes:
    """Read a base64-encoded key file.

    Args:
        path: Path to the key file.

    Returns:
        The raw key bytes.

    Raises:
        InventoryError: If the file is missing, unreadable, not valid base64,
            or does not decode to exactly 32 bytes.
    """
    try:
        encoded = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise InventoryError(f"Cannot read inventory key at {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise InventoryError(f"Inventory key at {path} is not ASCII base64: {exc}") from exc

    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InventoryError(f"Inventory key at {path} is not valid base64: {exc}") from exc

    if len(key) != KEY_BYTES:
        raise InventoryError(
            f"Inventory key at {path} decodes to {len(key)} bytes, expected {KEY_BYTES}"
        )
    return key


def load_or_create_key(path: Path) -> bytes:
    """Read an existing key, generating and persisting one if absent.

    Args:
        path: Path to the key file.

    Returns:
        The raw key bytes.

    Raises:
        InventoryError: If an existing file is present but invalid, or if a
            new key cannot be written.
    """
    if path.is_file():
        return load_key(path)
    _LOG.info("No inventory key at %s; generating a new AES-256 key", path)
    key = generate_key()
    write_key_file(key, path)
    return key


def encrypt_records(records: tuple[InventoryRecord, ...], key: bytes) -> bytes:
    """Encrypt inventory records into the on-disk wire format.

    Args:
        records: The records to encrypt.
        key: The 32-byte AES-256 key.

    Returns:
        Base64 ASCII bytes of ``nonce || ciphertext || tag``.

    Raises:
        InventoryError: If the key length is wrong or the records cannot be
            serialised to JSON.
    """
    if len(key) != KEY_BYTES:
        raise InventoryError(f"AES-256 key must be {KEY_BYTES} bytes, got {len(key)}")

    try:
        plaintext = json.dumps(
            [record.to_dict() for record in records], indent=2, sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InventoryError(f"Cannot serialise inventory records: {exc}") from exc

    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + sealed)


def decrypt_records(blob: bytes, key: bytes) -> tuple[InventoryRecord, ...]:
    """Decrypt and parse the on-disk inventory payload.

    Args:
        blob: Base64 ASCII bytes as produced by :func:`encrypt_records`.
        key: The 32-byte AES-256 key.

    Returns:
        The decrypted records in stored order.

    Raises:
        DecryptionError: If the payload is not valid base64, is shorter than a
            nonce plus tag, or fails GCM tag authentication. Authentication
            failure means the wrong key was used or the payload was altered.
        InventoryError: If the decrypted plaintext is not a JSON array of
            well-formed records.
    """
    if len(key) != KEY_BYTES:
        raise InventoryError(f"AES-256 key must be {KEY_BYTES} bytes, got {len(key)}")

    try:
        raw = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DecryptionError(f"Inventory payload is not valid base64: {exc}") from exc

    if len(raw) < NONCE_BYTES + TAG_BYTES:
        raise DecryptionError(
            f"Inventory payload is {len(raw)} bytes, too short to contain a "
            f"{NONCE_BYTES}-byte nonce and {TAG_BYTES}-byte tag"
        )

    nonce, sealed = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
    try:
        plaintext = AESGCM(key).decrypt(nonce, sealed, None)
    except InvalidTag as exc:
        raise DecryptionError(
            "Inventory payload failed AES-256-GCM authentication; the key is wrong "
            "or the ciphertext was truncated, corrupted, or tampered with"
        ) from exc

    try:
        decoded = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InventoryError(f"Decrypted inventory is not valid UTF-8 JSON: {exc}") from exc

    if not isinstance(decoded, list):
        raise InventoryError(
            f"Decrypted inventory must be a JSON array, got {type(decoded).__name__}"
        )

    records: list[InventoryRecord] = []
    for index, entry in enumerate(decoded):
        if not isinstance(entry, dict):
            raise InventoryError(f"Inventory entry {index} is not a JSON object")
        records.append(InventoryRecord.from_dict(entry))
    return tuple(records)


def upsert(
    records: tuple[InventoryRecord, ...], candidate: InventoryRecord
) -> tuple[InventoryRecord, ...]:
    """Insert a record, replacing any existing entry with the same hostname.

    Args:
        records: The current registry contents.
        candidate: The record to insert or replace.

    Returns:
        The updated registry. Replacement preserves the original position so
        balancer member ordering stays stable across re-registrations.
    """
    updated = list(records)
    for index, existing in enumerate(updated):
        if existing.hostname == candidate.hostname:
            updated[index] = candidate
            return tuple(updated)
    updated.append(candidate)
    return tuple(updated)
