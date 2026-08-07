"""Shared pytest fixtures for the cluster test suite.

The suite is split into two tiers. Unit tests exercise the cryptography and
configuration layers in process and always run. End-to-end tests need a live
provisioned cluster and are skipped, not failed, when one is not reachable, so
the suite stays useful on a workstation that has no cluster running.

Reachability is probed once per session and cached. Without that, every
end-to-end test would pay a TCP timeout against a cluster that is down.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

import allure
import pytest

from vmdeploy.config import ClusterConfig, load_cluster_config
from vmdeploy.exceptions import ConfigurationError, InventoryError
from vmdeploy.inventory import load_key

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

_DEFAULT_CONFIG: Final[Path] = Path("config/cluster.toml")
_PROBE_TIMEOUT_SECONDS: Final[float] = 3.0


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register command line options for the suite.

    Args:
        parser: The pytest option parser.
    """
    group = parser.getgroup("vmdeploy")
    group.addoption(
        "--cluster-config",
        action="store",
        default=str(_DEFAULT_CONFIG),
        help=f"path to the cluster TOML configuration (default: {_DEFAULT_CONFIG})",
    )
    group.addoption(
        "--balancer-url",
        action="store",
        default="",
        help="override the load balancer base URL instead of deriving it from config",
    )
    group.addoption(
        "--inventory-url",
        action="store",
        default="",
        help="override the inventory service base URL instead of deriving it from config",
    )
    group.addoption(
        "--require-cluster",
        action="store_true",
        default=False,
        help="fail instead of skipping when the cluster is unreachable",
    )


def _port_open(host: str, port: int, timeout: float = _PROBE_TIMEOUT_SECONDS) -> bool:
    """Check whether a TCP port accepts connections.

    Args:
        host: Hostname or IP address.
        port: TCP port number.
        timeout: Connection timeout in seconds.

    Returns:
        True if a TCP connection was established.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _endpoint(url: str) -> tuple[str, int]:
    """Split a base URL into host and port.

    Args:
        url: The base URL to split.

    Returns:
        The hostname and port, defaulting the port by scheme.

    Raises:
        ConfigurationError: If the URL has no hostname.
    """
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ConfigurationError(f"Cannot determine a hostname from URL {url!r}")
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


@pytest.fixture(scope="session")
def cluster_config(request: pytest.FixtureRequest) -> ClusterConfig:
    """Load the cluster configuration once per session.

    Args:
        request: The pytest request object.

    Returns:
        The validated cluster configuration.
    """
    path = Path(str(request.config.getoption("--cluster-config")))
    try:
        return load_cluster_config(path)
    except ConfigurationError as exc:
        raise AssertionError(f"Cannot load cluster configuration from {path}: {exc}") from exc


@pytest.fixture(scope="session")
def backend_hostnames(cluster_config: ClusterConfig) -> tuple[str, ...]:
    """Return the configured backend hostnames.

    Args:
        cluster_config: The cluster configuration.

    Returns:
        Backend hostnames in balancer member order.
    """
    return tuple(backend.hostname for backend in cluster_config.backends)


@pytest.fixture(scope="session")
def balancer_url(request: pytest.FixtureRequest, cluster_config: ClusterConfig) -> str:
    """Resolve the load balancer base URL.

    Args:
        request: The pytest request object.
        cluster_config: The cluster configuration.

    Returns:
        The balancer base URL without a trailing slash.
    """
    override = str(request.config.getoption("--balancer-url")).strip()
    if override:
        return override.rstrip("/")
    jump = cluster_config.jump
    port = "" if jump.http_port == 80 else f":{jump.http_port}"
    return f"http://{jump.hostname}{port}"


@pytest.fixture(scope="session")
def inventory_url(request: pytest.FixtureRequest, cluster_config: ClusterConfig) -> str:
    """Resolve the inventory service base URL.

    Args:
        request: The pytest request object.
        cluster_config: The cluster configuration.

    Returns:
        The inventory service base URL without a trailing slash.
    """
    override = str(request.config.getoption("--inventory-url")).strip()
    if override:
        return override.rstrip("/")
    return f"http://{cluster_config.jump.hostname}:{cluster_config.inventory.service_port}"


@pytest.fixture(scope="session")
def live_balancer(request: pytest.FixtureRequest, balancer_url: str) -> str:
    """Skip the test unless the load balancer is reachable.

    Args:
        request: The pytest request object.
        balancer_url: The balancer base URL.

    Returns:
        The balancer base URL.
    """
    host, port = _endpoint(balancer_url)
    if not _port_open(host, port):
        _unreachable(request, f"load balancer at {balancer_url}")
    return balancer_url


@pytest.fixture(scope="session")
def live_inventory(request: pytest.FixtureRequest, inventory_url: str) -> str:
    """Skip the test unless the inventory service is reachable.

    Args:
        request: The pytest request object.
        inventory_url: The inventory service base URL.

    Returns:
        The inventory service base URL.
    """
    host, port = _endpoint(inventory_url)
    if not _port_open(host, port):
        _unreachable(request, f"inventory service at {inventory_url}")
    return inventory_url


def _unreachable(request: pytest.FixtureRequest, what: str) -> None:
    """Skip or fail depending on the ``--require-cluster`` option.

    Args:
        request: The pytest request object.
        what: Human-readable description of the unreachable endpoint.

    Raises:
        Failed: If ``--require-cluster`` was passed.
        Skipped: Otherwise.
    """
    message = (
        f"No {what}. Provision the cluster with 'vmdeploy deploy', "
        "or pass --require-cluster to turn this skip into a failure."
    )
    if request.config.getoption("--require-cluster"):
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
def inventory_key(cluster_config: ClusterConfig) -> bytes:
    """Load the AES-256 inventory key, skipping if it is absent.

    Args:
        cluster_config: The cluster configuration.

    Returns:
        The raw 32-byte key.
    """
    path = cluster_config.inventory.key_file
    try:
        return load_key(path)
    except InventoryError as exc:
        # pytest.skip never returns, but pylint does not model NoReturn here.
        pytest.skip(f"Inventory key unavailable at {path}: {exc}")
        raise  # pragma: no cover


@pytest.fixture
def js_errors(page: object) -> Iterator[list[str]]:
    """Capture uncaught JavaScript errors raised during a Playwright test.

    Attaching a listener rather than asserting after the fact means a runtime
    error on a page that still renders correctly is caught, instead of passing
    silently because the visible DOM happened to look right.

    Args:
        page: The Playwright page fixture.

    Yields:
        A list that accumulates error messages as the page runs.
    """
    errors: list[str] = []

    def record(error: object) -> None:
        """Record one page error.

        Args:
            error: The Playwright error object.
        """
        errors.append(str(error))

    listener = getattr(page, "on", None)
    if callable(listener):
        listener("pageerror", record)

    yield errors

    if errors:
        allure.attach(
            "\n".join(errors),
            name="Uncaught JavaScript errors",
            attachment_type=allure.attachment_type.TEXT,
        )
