"""End-to-end validation of the Go encrypted inventory service.

The decisive test in this module is the pairing of ``/api/inventory`` with
``/api/inventory/raw``: the first proves the service can decrypt its datastore,
the second proves that what sits on disk is genuinely ciphertext. Either alone
is insufficient. A service that returned correct JSON from a plaintext file
would pass the first, and a file full of random bytes would pass the second.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Final

import allure
import pytest
import requests
from requests.exceptions import RequestException

from vmdeploy.config import ClusterConfig
from vmdeploy.exceptions import DecryptionError
from vmdeploy.inventory import decrypt_records, generate_key

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS: Final[int] = 15


def _get(url: str, *, expect_json: bool = True) -> Any:
    """Fetch a URL and optionally decode the JSON body.

    Args:
        url: The URL to fetch.
        expect_json: Whether to decode the body as JSON.

    Returns:
        The decoded JSON payload, or the raw response object.

    Raises:
        AssertionError: If the request fails, returns non-200, or the body is
            not decodable JSON when one was expected.
    """
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    except RequestException as exc:
        raise AssertionError(f"Request to {url} failed: {exc}") from exc

    assert response.status_code == 200, (
        f"{url} returned HTTP {response.status_code}: {response.text[:300]!r}"
    )
    if not expect_json:
        return response

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{url} did not return valid JSON: {response.text[:300]!r}") from exc


@allure.epic("Cluster Infrastructure")
@allure.feature("Encrypted Inventory Service")
@allure.story("Service availability")
@pytest.mark.e2e
@pytest.mark.inventory
class TestServiceAvailability:
    """The service is reachable and reports itself healthy."""

    @allure.title("The health endpoint reports ok")
    def test_health(self, live_inventory: str) -> None:
        """A liveness probe must succeed independently of datastore state.

        Args:
            live_inventory: The reachable inventory service base URL.
        """
        response = _get(f"{live_inventory}/healthz", expect_json=False)
        assert response.text.strip() == "ok"

    @allure.title("Unsupported methods are rejected with 405 and an Allow header")
    def test_method_not_allowed(self, live_inventory: str) -> None:
        """The service advertises permitted methods rather than failing opaquely.

        Args:
            live_inventory: The reachable inventory service base URL.
        """
        try:
            response = requests.delete(
                f"{live_inventory}/api/inventory", timeout=REQUEST_TIMEOUT_SECONDS
            )
        except RequestException as exc:
            pytest.fail(f"DELETE probe failed: {exc}")

        assert response.status_code == 405
        assert "Allow" in response.headers


@allure.epic("Cluster Infrastructure")
@allure.feature("Encrypted Inventory Service")
@allure.story("Encryption at rest")
@pytest.mark.e2e
@pytest.mark.inventory
class TestEncryptionAtRest:
    """The stored datastore is genuinely encrypted, not merely encoded."""

    @allure.title("The raw datastore contains no readable hostname")
    @allure.severity(allure.severity_level.CRITICAL)
    def test_raw_payload_is_opaque(
        self, live_inventory: str, cluster_config: ClusterConfig
    ) -> None:
        """Neither the raw bytes nor a base64 decode may reveal cluster hosts.

        Args:
            live_inventory: The reachable inventory service base URL.
            cluster_config: The cluster configuration.
        """
        response = _get(f"{live_inventory}/api/inventory/raw", expect_json=False)
        raw = response.content
        assert raw.strip(), "the datastore is empty; provision the cluster before testing"

        hostnames = [host.hostname for host in cluster_config.all_hosts]

        with allure.step("Assert hostnames are absent from the stored payload"):
            for hostname in hostnames:
                assert hostname.encode() not in raw, (
                    f"hostname {hostname!r} is readable in the stored payload"
                )

        with allure.step("Assert base64 decoding alone does not reveal hostnames"):
            try:
                decoded = base64.b64decode(raw, validate=True)
            except (ValueError, base64.binascii.Error) as exc:
                pytest.fail(f"stored payload is not valid base64: {exc}")

            for hostname in hostnames:
                assert hostname.encode() not in decoded, (
                    f"hostname {hostname!r} is recoverable by decoding alone; the "
                    "datastore is encoded, not encrypted"
                )

    @allure.title("The raw datastore decrypts with the configured key")
    @allure.severity(allure.severity_level.CRITICAL)
    def test_raw_payload_decrypts(self, live_inventory: str, inventory_key: bytes) -> None:
        """The ciphertext must open under the key the deployment installed.

        Args:
            live_inventory: The reachable inventory service base URL.
            inventory_key: The configured AES-256 key.
        """
        response = _get(f"{live_inventory}/api/inventory/raw", expect_json=False)

        with allure.step("Decrypt the stored payload with the deployment key"):
            records = decrypt_records(response.content, inventory_key)

        allure.attach(
            "\n".join(
                f"{record.hostname} {record.role} {record.ipv4} {record.state}"
                for record in records
            ),
            name="Decrypted inventory",
            attachment_type=allure.attachment_type.TEXT,
        )
        assert records, "the decrypted inventory is empty"

    @allure.title("The raw datastore does not decrypt under an unrelated key")
    def test_wrong_key_is_rejected(self, live_inventory: str) -> None:
        """Confirms the payload is bound to its key, not trivially readable.

        Args:
            live_inventory: The reachable inventory service base URL.
        """
        response = _get(f"{live_inventory}/api/inventory/raw", expect_json=False)

        with pytest.raises(DecryptionError):
            decrypt_records(response.content, generate_key())


@allure.epic("Cluster Infrastructure")
@allure.feature("Encrypted Inventory Service")
@allure.story("Inventory retrieval")
@pytest.mark.e2e
@pytest.mark.inventory
class TestInventoryRetrieval:
    """The decrypted API view matches the cluster and the stored ciphertext."""

    @allure.title("The API lists every configured cluster host")
    @allure.severity(allure.severity_level.CRITICAL)
    def test_api_lists_all_hosts(
        self, live_inventory: str, cluster_config: ClusterConfig
    ) -> None:
        """Every provisioned host must appear in the registry.

        Args:
            live_inventory: The reachable inventory service base URL.
            cluster_config: The cluster configuration.
        """
        payload = _get(f"{live_inventory}/api/inventory")
        assert isinstance(payload, list), f"expected a JSON array, got {type(payload).__name__}"

        reported = {str(entry.get("hostname", "")) for entry in payload}
        expected = {host.hostname for host in cluster_config.all_hosts}

        allure.attach(
            json.dumps(payload, indent=2),
            name="Inventory API response",
            attachment_type=allure.attachment_type.JSON,
        )

        missing = expected - reported
        assert not missing, f"inventory is missing host(s): {sorted(missing)}"

    @allure.title("The API view matches the decrypted ciphertext exactly")
    def test_api_matches_ciphertext(self, live_inventory: str, inventory_key: bytes) -> None:
        """The served view must be derived from the stored datastore.

        A mismatch would mean the service is serving a cached or in-memory
        registry that has drifted from what is persisted.

        Args:
            live_inventory: The reachable inventory service base URL.
            inventory_key: The configured AES-256 key.
        """
        api_payload = _get(f"{live_inventory}/api/inventory")
        raw = _get(f"{live_inventory}/api/inventory/raw", expect_json=False).content
        decrypted = decrypt_records(raw, inventory_key)

        api_hosts = sorted(str(entry.get("hostname", "")) for entry in api_payload)
        stored_hosts = sorted(record.hostname for record in decrypted)

        assert api_hosts == stored_hosts, (
            f"API view {api_hosts} disagrees with the stored datastore {stored_hosts}"
        )

    @allure.title("Each record carries a plausible IPv4 address and active state")
    def test_records_are_populated(
        self, live_inventory: str, cluster_config: ClusterConfig
    ) -> None:
        """Registry entries must be usable, not merely present.

        Args:
            live_inventory: The reachable inventory service base URL.
            cluster_config: The cluster configuration.
        """
        payload = _get(f"{live_inventory}/api/inventory")
        by_host = {str(entry.get("hostname", "")): entry for entry in payload}

        for host in cluster_config.all_hosts:
            entry = by_host.get(host.hostname)
            assert entry is not None, f"{host.hostname} is absent from the inventory"

            with allure.step(f"Validate the record for {host.hostname}"):
                assert entry.get("role") == host.role.value, (
                    f"{host.hostname} has role {entry.get('role')!r}, "
                    f"expected {host.role.value!r}"
                )
                ipv4 = str(entry.get("ipv4", ""))
                assert ipv4.count(".") == 3 and ipv4.replace(".", "").isdigit(), (
                    f"{host.hostname} has an implausible IPv4 address {ipv4!r}"
                )
                assert entry.get("status") == "Deployed"
                assert entry.get("state") == "Active"


@allure.epic("Cluster Infrastructure")
@allure.feature("Encrypted Inventory Service")
@allure.story("Cluster view rendering")
@pytest.mark.e2e
@pytest.mark.inventory
class TestClusterView:
    """The HTML cluster view renders the decrypted registry."""

    @allure.title("The cluster view renders every host in the browser")
    def test_cluster_view_renders_hosts(
        self,
        page: object,
        live_inventory: str,
        cluster_config: ClusterConfig,
        js_errors: list[str],
    ) -> None:
        """The rendered table must list the cluster, without script errors.

        Args:
            page: The Playwright page fixture.
            live_inventory: The reachable inventory service base URL.
            cluster_config: The cluster configuration.
            js_errors: Accumulated uncaught JavaScript errors.
        """
        goto = getattr(page, "goto", None)
        content = getattr(page, "content", None)
        if not callable(goto) or not callable(content):
            pytest.fail("Playwright page fixture is missing goto/content")

        with allure.step(f"Open {live_inventory}/clusterview"):
            try:
                response = goto(f"{live_inventory}/clusterview", wait_until="domcontentloaded")
            # pylint: disable-next=broad-exception-caught
            except Exception as exc:  # Playwright raises a broad Error hierarchy.
                pytest.fail(f"Could not open the cluster view: {exc}")

        status = getattr(response, "status", None)
        if isinstance(status, int):
            assert status == 200, f"cluster view returned HTTP {status}"

        rendered = str(content())
        allure.attach(
            rendered, name="Cluster view HTML", attachment_type=allure.attachment_type.HTML
        )

        with allure.step("Assert the page raised no uncaught JavaScript errors"):
            assert not js_errors, f"cluster view raised JavaScript errors: {js_errors}"

        with allure.step("Assert every cluster host appears in the rendered table"):
            missing = [
                host.hostname
                for host in cluster_config.all_hosts
                if host.hostname not in rendered
            ]
            assert not missing, f"cluster view does not render host(s): {missing}"


@allure.epic("Cluster Infrastructure")
@allure.feature("Encrypted Inventory Service")
@allure.story("Registry updates")
@pytest.mark.e2e
@pytest.mark.inventory
class TestRegistryUpdates:
    """The service validates and persists record upserts."""

    @allure.title("A malformed record is rejected with HTTP 400")
    @pytest.mark.parametrize(
        ("payload", "reason"),
        [
            ({"role": "backend"}, "missing hostname"),
            ({"hostname": "apnode9", "role": "database"}, "invalid role"),
            ({"hostname": "apnode9", "role": "backend", "bogus": 1}, "unknown field"),
        ],
    )
    def test_rejects_malformed_records(
        self, live_inventory: str, payload: dict[str, Any], reason: str
    ) -> None:
        """Invalid submissions must not enter the registry.

        Args:
            live_inventory: The reachable inventory service base URL.
            payload: The malformed record under test.
            reason: Human-readable description of the defect.
        """
        try:
            response = requests.post(
                f"{live_inventory}/api/inventory",
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except RequestException as exc:
            pytest.fail(f"POST probe failed: {exc}")

        assert response.status_code == 400, (
            f"expected HTTP 400 for {reason}, got {response.status_code}: "
            f"{response.text[:200]!r}"
        )

    @allure.title("An upserted record is persisted and re-encrypted")
    def test_upsert_round_trips_through_ciphertext(
        self, live_inventory: str, inventory_key: bytes
    ) -> None:
        """A posted record must survive into the encrypted datastore.

        Args:
            live_inventory: The reachable inventory service base URL.
            inventory_key: The configured AES-256 key.
        """
        probe = {
            "hostname": "apnode-testprobe",
            "role": "backend",
            "ipv4": "203.0.113.9",
            "ipv6": "",
            "status": "Deployed",
            "status_timestamp": "",
            "state": "Inactive",
            "state_timestamp": "",
        }

        with allure.step("Post a probe record"):
            try:
                response = requests.post(
                    f"{live_inventory}/api/inventory",
                    json=probe,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except RequestException as exc:
                pytest.fail(f"upsert failed: {exc}")
            assert response.status_code == 200, response.text[:300]

        with allure.step("Confirm the probe is present in the re-encrypted datastore"):
            raw = _get(f"{live_inventory}/api/inventory/raw", expect_json=False).content
            records = decrypt_records(raw, inventory_key)
            stored = {record.hostname: record for record in records}

            assert "apnode-testprobe" in stored, (
                f"probe record was not persisted; stored hosts: {sorted(stored)}"
            )
            assert stored["apnode-testprobe"].ipv4 == "203.0.113.9"

        with allure.step("Confirm the service filled in the omitted timestamps"):
            assert stored["apnode-testprobe"].status_timestamp, "status timestamp was not set"
            assert stored["apnode-testprobe"].state_timestamp, "state timestamp was not set"
