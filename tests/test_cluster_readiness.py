"""Unit tests for the post-configure readiness gate.

``systemctl is-active`` returns as soon as systemd forks a process, which is
before Apache accepts connections. Reporting configure as complete at that point
made the documented next step, running the E2E suite, race the services under
test: a first deploy failed intermittently and passed on a re-run a minute
later. These tests pin the gate that closed that race, including the timeout
path, because a readiness check that hangs forever would be worse than the race
it replaced.
"""

from __future__ import annotations

from pathlib import Path

import allure
import pytest
import requests

from vmdeploy.config import load_cluster_config
from vmdeploy.exceptions import ProvisioningTimeoutError
from vmdeploy.provision import wait_for_cluster_ready

_BASE = """
[ssh]
user = "vmadmin"
key_path = "~/.vmdeploy/keys/vm_key"
connect_timeout_seconds = 30
command_timeout_seconds = 300

[virtualbox]
vboxmanage = "{vboxmanage}"
template_vm = "apubuntuD"
template_ova = "~/.vmdeploy/golden.ova"
memory_mb = 1024
cpus = 1
boot_timeout_seconds = 420

[jump]
vm_name = "apjump"
hostname = "apjump"
http_port = 80

[[backend]]
vm_name = "apnode1"
hostname = "apnode1"
http_port = 8080

[[backend]]
vm_name = "apnode2"
hostname = "apnode2"
http_port = 8080

[inventory]
service_port = 5090
remote_dir = "/opt/inventory"
key_file = "~/.vmdeploy/keys/inventory.key"
datastore_name = "inventory.enc"
manifest_file = "~/.vmdeploy/inventory.enc"

[website]
source_url = "https://example.github.io/"
archive_url = "https://example.invalid/main.tar.gz"
document_root = "/var/www/html"

[build]
go_version = "1.26.5"
go_sha256 = "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"
"""


class _Response:
    """A minimal stand-in for a requests Response."""

    def __init__(self, status_code: int) -> None:
        """Record the status code the caller should see.

        Args:
            status_code: The HTTP status to report.
        """
        self.status_code = status_code

    @property
    def ok(self) -> bool:
        """Report success for 2xx and 3xx, as requests does.

        Returns:
            True when the status is below 400.
        """
        return self.status_code < 400


@pytest.fixture(name="config")
def _config(tmp_path: Path):
    """Build a loaded configuration for the readiness gate.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        A parsed cluster configuration.
    """
    base = tmp_path / "cluster.toml"
    base.write_text(_BASE.format(vboxmanage=(tmp_path / "vb").as_posix()), encoding="utf-8")
    return load_cluster_config(base)


@allure.epic("Cluster Infrastructure")
@allure.feature("Provisioning")
@allure.story("Post-configure readiness")
class TestClusterReadiness:
    """Configure must not report success before the cluster serves traffic."""

    @allure.title("Both endpoints are polled before returning")
    def test_polls_balancer_and_inventory(
        self, config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ready balancer alone is not enough; the inventory is checked too.

        Args:
            config: The cluster configuration fixture.
            monkeypatch: Pytest monkeypatch fixture.
        """
        seen: list[str] = []

        # timeout is unused but must be accepted: it mirrors requests.get, so a
        # caller that stops passing it would still be exercised faithfully here.
        def fake_get(url: str, timeout: int = 0) -> _Response:  # pylint: disable=unused-argument
            seen.append(url)
            return _Response(200)

        monkeypatch.setattr(requests, "get", fake_get)
        wait_for_cluster_ready(config, "192.0.2.10", timeout_seconds=5)

        assert "http://192.0.2.10/" in seen
        assert "http://192.0.2.10:5090/healthz" in seen

    @allure.title("A service that never answers raises rather than hanging")
    def test_timeout_raises(self, config, monkeypatch: pytest.MonkeyPatch) -> None:
        """The deploy must fail loudly instead of blocking forever.

        Args:
            config: The cluster configuration fixture.
            monkeypatch: Pytest monkeypatch fixture.
        """

        def always_refused(url: str, timeout: int = 0) -> _Response:
            raise requests.ConnectionError("connection refused")

        monkeypatch.setattr(requests, "get", always_refused)
        monkeypatch.setattr("vmdeploy.provision.time.sleep", lambda _seconds: None)

        with pytest.raises(ProvisioningTimeoutError, match="load balancer"):
            wait_for_cluster_ready(config, "192.0.2.10", timeout_seconds=1)

    @allure.title("An HTTP error keeps polling rather than passing")
    def test_error_status_is_not_ready(
        self, config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 503 from a still-starting Apache must not count as ready.

        Args:
            config: The cluster configuration fixture.
            monkeypatch: Pytest monkeypatch fixture.
        """
        monkeypatch.setattr(requests, "get", lambda url, timeout=0: _Response(503))
        monkeypatch.setattr("vmdeploy.provision.time.sleep", lambda _seconds: None)

        with pytest.raises(ProvisioningTimeoutError, match="HTTP 503"):
            wait_for_cluster_ready(config, "192.0.2.10", timeout_seconds=1)
