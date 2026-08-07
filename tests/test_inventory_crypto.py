"""Unit tests for the AES-256-GCM inventory encryption layer.

These run in process and need no cluster, so the cryptographic contract is
verified on every run rather than only when infrastructure happens to be up.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import allure
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vmdeploy.exceptions import DecryptionError, InventoryError
from vmdeploy.inventory import (
    KEY_BYTES,
    NONCE_BYTES,
    InventoryRecord,
    decrypt_records,
    encrypt_records,
    generate_key,
    load_key,
    load_or_create_key,
    upsert,
    write_key_file,
)


def _record(hostname: str, role: str = "backend") -> InventoryRecord:
    """Build a populated inventory record for testing.

    Args:
        hostname: The record's hostname.
        role: The record's role.

    Returns:
        A fully populated record.
    """
    return InventoryRecord(
        hostname=hostname,
        role=role,
        ipv4="192.168.1.80",
        ipv6="fe80::1",
        status="Deployed",
        status_timestamp="2026-08-05T10:00:00Z",
        state="Active",
        state_timestamp="2026-08-05T10:00:00Z",
    )


@allure.epic("Cluster Infrastructure")
@allure.feature("Encrypted Inventory")
@allure.story("Encryption round trip")
@pytest.mark.inventory
class TestEncryptionRoundTrip:
    """Encryption and decryption preserve inventory contents exactly."""

    @allure.title("Records survive an encrypt/decrypt round trip unchanged")
    def test_round_trip_preserves_records(self) -> None:
        """Encrypting then decrypting returns the original records."""
        key = generate_key()
        original = (_record("apjump", "jump"), _record("apnode1"), _record("apnode2"))

        with allure.step("Encrypt three inventory records"):
            blob = encrypt_records(original, key)

        with allure.step("Decrypt the payload with the same key"):
            recovered = decrypt_records(blob, key)

        assert recovered == original, "round trip altered the inventory records"

    @allure.title("Ciphertext does not leak plaintext hostnames")
    def test_ciphertext_hides_plaintext(self) -> None:
        """The stored payload must not contain readable record content.

        This is the property the base64-only reference implementation failed:
        its 'encrypted' datastore contained every hostname in clear text after
        a single decode.
        """
        key = generate_key()
        blob = encrypt_records((_record("secret-host"),), key)

        with allure.step("Assert the hostname is absent from the raw payload"):
            assert b"secret-host" not in blob

        with allure.step("Assert base64 decoding alone does not reveal it"):
            decoded = base64.b64decode(blob)
            assert b"secret-host" not in decoded, (
                "hostname is recoverable by decoding alone; the payload is encoded, "
                "not encrypted"
            )

    @allure.title("Encrypting identical records twice yields different ciphertext")
    def test_nonce_is_fresh_per_encryption(self) -> None:
        """A fresh nonce per encryption prevents ciphertext correlation."""
        key = generate_key()
        records = (_record("apnode1"),)

        first = encrypt_records(records, key)
        second = encrypt_records(records, key)

        assert first != second, "identical plaintext produced identical ciphertext"
        assert decrypt_records(first, key) == decrypt_records(second, key)

    @allure.title("An empty inventory round trips as an empty tuple")
    def test_empty_inventory_round_trips(self) -> None:
        """An empty registry is a valid state before the first host registers."""
        key = generate_key()
        assert not decrypt_records(encrypt_records((), key), key)


@allure.epic("Cluster Infrastructure")
@allure.feature("Encrypted Inventory")
@allure.story("Decryption failure handling")
@pytest.mark.inventory
class TestDecryptionFailures:
    """Every corruption mode raises a typed, actionable error."""

    @allure.title("Decrypting with the wrong key raises DecryptionError")
    def test_wrong_key_rejected(self) -> None:
        """A key mismatch must fail authentication rather than return garbage."""
        blob = encrypt_records((_record("apnode1"),), generate_key())

        with allure.step("Attempt decryption with an unrelated key"):
            with pytest.raises(DecryptionError, match="authentication"):
                decrypt_records(blob, generate_key())

    @allure.title("Tampered ciphertext fails the GCM authentication tag")
    def test_tampered_ciphertext_rejected(self) -> None:
        """Flipping one bit past the nonce must be detected."""
        key = generate_key()
        raw = bytearray(base64.b64decode(encrypt_records((_record("apnode1"),), key)))
        raw[NONCE_BYTES + 1] ^= 0x01
        tampered = base64.b64encode(bytes(raw))

        with pytest.raises(DecryptionError):
            decrypt_records(tampered, key)

    @allure.title("Truncated payloads are rejected before decryption is attempted")
    def test_truncated_payload_rejected(self) -> None:
        """A payload too short to hold a nonce and tag is structurally invalid."""
        with pytest.raises(DecryptionError, match="too short"):
            decrypt_records(base64.b64encode(b"short"), generate_key())

    @allure.title("Non-base64 payloads raise DecryptionError")
    def test_invalid_base64_rejected(self) -> None:
        """Malformed encoding is reported distinctly from a bad key."""
        with pytest.raises(DecryptionError, match="base64"):
            decrypt_records(b"this is not base64 !!!", generate_key())

    @allure.title("A wrong-length key is rejected before any cipher work")
    @pytest.mark.parametrize("length", [0, 16, 31, 33, 64])
    def test_wrong_key_length_rejected(self, length: int) -> None:
        """AES-256 requires exactly 32 bytes.

        Args:
            length: The invalid key length under test.
        """
        with pytest.raises(InventoryError, match=str(KEY_BYTES)):
            encrypt_records((_record("apnode1"),), b"\x00" * length)

    @allure.title("Valid ciphertext wrapping non-record JSON raises InventoryError")
    def test_valid_ciphertext_with_wrong_schema(self) -> None:
        """Authentic ciphertext can still carry a payload of the wrong shape.

        This separates a cryptographic failure from a schema failure: the key
        was right and the tag verified, so the problem is the data, not the
        crypto, and the exception type must say so.
        """
        key = generate_key()
        nonce = b"\x00" * NONCE_BYTES
        payload = json.dumps({"not": "an array"}).encode("utf-8")
        blob = base64.b64encode(nonce + AESGCM(key).encrypt(nonce, payload, None))

        with pytest.raises(InventoryError, match="JSON array"):
            decrypt_records(blob, key)

    @allure.title("A record missing required fields is rejected")
    def test_missing_fields_rejected(self) -> None:
        """Incomplete records must not silently default."""
        with pytest.raises(InventoryError, match="missing required field"):
            InventoryRecord.from_dict({"hostname": "apnode1", "role": "backend"})


@allure.epic("Cluster Infrastructure")
@allure.feature("Encrypted Inventory")
@allure.story("Key management")
@pytest.mark.inventory
class TestKeyManagement:
    """Key files are generated, persisted, and validated correctly."""

    @allure.title("Generated keys are 32 bytes and unique")
    def test_generate_key(self) -> None:
        """Each generated key is the right length and distinct."""
        first, second = generate_key(), generate_key()
        assert len(first) == KEY_BYTES
        assert first != second

    @allure.title("A key survives a write and read cycle")
    def test_key_file_round_trip(self, tmp_path: Path) -> None:
        """Writing then reading a key returns the original bytes.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        key = generate_key()
        path = tmp_path / "inventory.key"
        write_key_file(key, path)
        assert load_key(path) == key

    @allure.title("load_or_create_key generates once and is stable afterwards")
    def test_load_or_create_is_idempotent(self, tmp_path: Path) -> None:
        """A second call returns the key created by the first.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        path = tmp_path / "nested" / "inventory.key"
        created = load_or_create_key(path)
        assert path.is_file()
        assert load_or_create_key(path) == created

    @allure.title("A malformed key file raises InventoryError")
    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ("not valid base64 !!!", "base64"),
            # An empty file is technically valid base64, so it is caught by the
            # length check rather than the decoder.
            ("", "0 bytes"),
            (base64.b64encode(b"\x00" * 16).decode("ascii"), "16 bytes"),
        ],
    )
    def test_malformed_key_rejected(self, tmp_path: Path, content: str, expected: str) -> None:
        """Bad key material is reported clearly rather than used.

        Args:
            tmp_path: Pytest-provided temporary directory.
            content: The malformed key file content under test.
            expected: Substring the error message must contain.
        """
        path = tmp_path / "bad.key"
        path.write_text(content, encoding="ascii")
        with pytest.raises(InventoryError, match=expected):
            load_key(path)

    @allure.title("A missing key file raises InventoryError")
    def test_missing_key_rejected(self, tmp_path: Path) -> None:
        """An absent key is an error for load_key, unlike load_or_create_key.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        with pytest.raises(InventoryError, match="Cannot read"):
            load_key(tmp_path / "absent.key")


@allure.epic("Cluster Infrastructure")
@allure.feature("Encrypted Inventory")
@allure.story("Registry updates")
@pytest.mark.inventory
class TestUpsert:
    """Records are inserted and replaced without disturbing member order."""

    @allure.title("A new hostname is appended")
    def test_appends_new_host(self) -> None:
        """An unseen hostname is added to the end."""
        records = (_record("apnode1"),)
        updated = upsert(records, _record("apnode2"))
        assert [record.hostname for record in updated] == ["apnode1", "apnode2"]

    @allure.title("An existing hostname is replaced in place")
    def test_replaces_in_place(self) -> None:
        """Re-registering a host preserves its position.

        Balancer member ordering is derived from this sequence, so a
        replacement that moved the record would silently reorder the pool.
        """
        records = (_record("apnode1"), _record("apnode2"), _record("apnode3"))
        replacement = InventoryRecord(
            hostname="apnode2",
            role="backend",
            ipv4="10.0.0.9",
            ipv6="",
            status="Deployed",
            status_timestamp="2026-08-05T11:00:00Z",
            state="Inactive",
            state_timestamp="2026-08-05T11:00:00Z",
        )

        updated = upsert(records, replacement)

        assert [record.hostname for record in updated] == ["apnode1", "apnode2", "apnode3"]
        assert updated[1].ipv4 == "10.0.0.9"
        assert updated[1].state == "Inactive"
