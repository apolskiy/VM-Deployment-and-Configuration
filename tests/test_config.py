"""Unit tests for cluster configuration loading and validation.

Provisioning destroys and recreates virtual machines, so a configuration
mistake is expensive. These tests pin the validation rules that catch such
mistakes before any hypervisor call is made.
"""

from __future__ import annotations

from pathlib import Path

import allure
import pytest

from vmdeploy.config import HostRole, load_cluster_config
from vmdeploy.exceptions import ConfigurationError

_VALID = """
[ssh]
user = "qauser"
key_path = "C:/keys/vm_key"
connect_timeout_seconds = 30
command_timeout_seconds = 300

[virtualbox]
vboxmanage = "C:/VBoxManage.exe"
template_vm = "apubuntuD"
template_ova = "C:/templates/golden.ova"
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
key_file = "C:/keys/inventory.key"
datastore_name = "inventory.enc"
manifest_file = "C:/keys/inventory.enc"

[website]
source_url = "https://example.github.io/"
archive_url = "https://example.invalid/main.tar.gz"
document_root = "/var/www/html"

[build]
go_version = "1.26.5"
go_sha256 = "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"
"""


def _write(tmp_path: Path, content: str) -> Path:
    """Write configuration content to a temporary file.

    Args:
        tmp_path: Pytest-provided temporary directory.
        content: TOML content to write.

    Returns:
        Path to the written file.
    """
    path = tmp_path / "cluster.toml"
    path.write_text(content, encoding="utf-8")
    return path


@allure.epic("Cluster Infrastructure")
@allure.feature("Configuration")
@allure.story("Valid configuration")
class TestValidConfiguration:
    """A well-formed document loads into a fully typed structure."""

    @allure.title("A complete configuration loads with correct roles and ordering")
    def test_loads_complete_configuration(self, tmp_path: Path) -> None:
        """All sections parse and roles are assigned correctly.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        config = load_cluster_config(_write(tmp_path, _VALID))

        assert config.jump.role is HostRole.JUMP
        assert all(backend.role is HostRole.BACKEND for backend in config.backends)
        assert [host.hostname for host in config.all_hosts] == ["apjump", "apnode1", "apnode2"]
        assert config.virtualbox.memory_mb == 1024
        assert config.inventory.service_port == 5090

    @allure.title("Backend declaration order is preserved as balancer member order")
    def test_backend_order_preserved(self, tmp_path: Path) -> None:
        """Member ordering must follow the document, not a set or dict.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        config = load_cluster_config(_write(tmp_path, _VALID))
        assert [backend.hostname for backend in config.backends] == ["apnode1", "apnode2"]


@allure.epic("Cluster Infrastructure")
@allure.feature("Configuration")
@allure.story("Validation failures")
class TestValidationFailures:
    """Malformed documents are rejected with actionable messages."""

    @allure.title("A missing file raises ConfigurationError")
    def test_missing_file(self, tmp_path: Path) -> None:
        """An absent configuration is reported clearly.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        with pytest.raises(ConfigurationError, match="Cannot read"):
            load_cluster_config(tmp_path / "absent.toml")

    @allure.title("Malformed TOML raises ConfigurationError")
    def test_invalid_toml(self, tmp_path: Path) -> None:
        """A syntax error is reported as a configuration problem.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        with pytest.raises(ConfigurationError, match="Invalid TOML"):
            load_cluster_config(_write(tmp_path, "this is not = = toml"))

    @allure.title("A missing section raises ConfigurationError")
    @pytest.mark.parametrize("section", ["ssh", "virtualbox", "jump", "inventory", "website"])
    def test_missing_section(self, tmp_path: Path, section: str) -> None:
        """Each required table is enforced.

        Args:
            tmp_path: Pytest-provided temporary directory.
            section: The table removed for this case.
        """
        lines = _VALID.splitlines()
        start = lines.index(f"[{section}]")
        end = start + 1
        while end < len(lines) and not lines[end].startswith("["):
            end += 1
        stripped = "\n".join(lines[:start] + lines[end:])

        with pytest.raises(ConfigurationError, match=section):
            load_cluster_config(_write(tmp_path, stripped))

    @allure.title("Fewer than two backends is rejected")
    def test_requires_two_backends(self, tmp_path: Path) -> None:
        """Distribution cannot be validated against a single member.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        single = _VALID.replace(
            '[[backend]]\nvm_name = "apnode2"\nhostname = "apnode2"\nhttp_port = 8080\n', ""
        )
        with pytest.raises(ConfigurationError, match="At least two"):
            load_cluster_config(_write(tmp_path, single))

    @allure.title("Duplicate VM names are rejected")
    def test_duplicate_vm_names(self, tmp_path: Path) -> None:
        """Two machines cannot share a VirtualBox name.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        duplicated = _VALID.replace('vm_name = "apnode2"', 'vm_name = "apnode1"')
        with pytest.raises(ConfigurationError, match="[Dd]uplicate"):
            load_cluster_config(_write(tmp_path, duplicated))

    @allure.title("A template that collides with a cluster VM is rejected")
    def test_template_collision(self, tmp_path: Path) -> None:
        """The golden template must stay separate from the cluster.

        Exporting a machine that is also a cluster member would overwrite the
        template with a configured node on the next run.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        collided = _VALID.replace('template_vm = "apubuntuD"', 'template_vm = "apjump"')
        with pytest.raises(ConfigurationError, match="collides"):
            load_cluster_config(_write(tmp_path, collided))

    @allure.title("An inventory port colliding with the balancer port is rejected")
    def test_port_collision(self, tmp_path: Path) -> None:
        """Two services cannot bind the same port on the jump station.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        collided = _VALID.replace("service_port = 5090", "service_port = 80")
        with pytest.raises(ConfigurationError, match="collides"):
            load_cluster_config(_write(tmp_path, collided))

    @allure.title("An out-of-range port is rejected")
    @pytest.mark.parametrize("port", [0, -1, 65536, 99999])
    def test_invalid_port(self, tmp_path: Path, port: int) -> None:
        """Ports must fall inside the valid TCP range.

        Args:
            tmp_path: Pytest-provided temporary directory.
            port: The invalid port under test.
        """
        # Anchor to the line end: "http_port = 80" is also a prefix of the
        # backends' "http_port = 8080".
        invalid = _VALID.replace("http_port = 80\n", f"http_port = {port}\n")
        with pytest.raises(ConfigurationError, match="http_port"):
            load_cluster_config(_write(tmp_path, invalid))

    @allure.title("Memory below the supported floor is rejected")
    def test_memory_floor(self, tmp_path: Path) -> None:
        """A guest too small to boot Ubuntu is rejected up front.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        invalid = _VALID.replace("memory_mb = 1024", "memory_mb = 128")
        with pytest.raises(ConfigurationError, match="memory_mb"):
            load_cluster_config(_write(tmp_path, invalid))

    @allure.title("A boolean supplied where an integer is required is rejected")
    def test_boolean_is_not_an_integer(self, tmp_path: Path) -> None:
        """Booleans subclass int in Python and must be rejected explicitly.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        invalid = _VALID.replace("cpus = 1", "cpus = true")
        with pytest.raises(ConfigurationError, match="cpus"):
            load_cluster_config(_write(tmp_path, invalid))

    @allure.title("An empty required string is rejected")
    def test_blank_string_rejected(self, tmp_path: Path) -> None:
        """Whitespace-only values must not pass as present.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        invalid = _VALID.replace('user = "qauser"', 'user = "   "')
        with pytest.raises(ConfigurationError, match="user"):
            load_cluster_config(_write(tmp_path, invalid))
