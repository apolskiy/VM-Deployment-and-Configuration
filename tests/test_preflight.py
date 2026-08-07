"""Unit tests for pre-deployment checks and portable path expansion.

Preflight must fail loudly on a genuinely unusable host (missing tools, no key,
no image) and must never crash on inputs it cannot measure. Path expansion must
make one configuration portable across users and machines.
"""

from __future__ import annotations

import os
from pathlib import Path

import allure
import pytest

from vmdeploy.config import ClusterConfig, load_cluster_config
from vmdeploy.exceptions import ConfigurationError
from vmdeploy.preflight import (
    CheckStatus,
    check_ssh_key,
    check_template_or_ova,
    check_vboxmanage,
    format_report,
    run_preflight,
    worst_status,
)

_TEMPLATE = """
[ssh]
user = "qauser"
key_path = "{key_path}"
connect_timeout_seconds = 30
command_timeout_seconds = 300

[virtualbox]
vboxmanage = "{vboxmanage}"
template_vm = "apubuntuD"
template_ova = "{template_ova}"
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
key_file = "{key_file}"
datastore_name = "inventory.enc"
manifest_file = "{manifest_file}"

[website]
source_url = "https://example.github.io/"
archive_url = "https://example.invalid/main.tar.gz"
document_root = "/var/www/html"

[build]
go_version = "1.26.5"
go_sha256 = "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"
"""


def _config(tmp_path: Path, **overrides: str) -> ClusterConfig:
    """Build a config whose paths default under tmp_path.

    Args:
        tmp_path: Pytest-provided temporary directory.
        **overrides: Field values to substitute into the template.

    Returns:
        The loaded configuration.
    """
    values = {
        "key_path": (tmp_path / "vm_key").as_posix(),
        "vboxmanage": (tmp_path / "VBoxManage.exe").as_posix(),
        "template_ova": (tmp_path / "golden.ova").as_posix(),
        "key_file": (tmp_path / "inventory.key").as_posix(),
        "manifest_file": (tmp_path / "inventory.enc").as_posix(),
    }
    values.update(overrides)
    path = tmp_path / "cluster.toml"
    path.write_text(_TEMPLATE.format(**values), encoding="utf-8")
    return load_cluster_config(path)


@allure.epic("Cluster Infrastructure")
@allure.feature("Portability")
@allure.story("Path expansion")
class TestPathExpansion:
    """Configuration paths expand ``~`` and environment variables."""

    @allure.title("A leading ~ expands to the user's home directory")
    def test_tilde_expands(self, tmp_path: Path) -> None:
        """``~`` must not survive into a resolved path.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        config = _config(tmp_path, key_path="~/keys/vm_key")
        assert "~" not in str(config.ssh.key_path)
        assert config.ssh.key_path == Path.home() / "keys" / "vm_key"

    @allure.title("An environment variable in a path is expanded")
    def test_env_var_expands(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A set variable resolves; the raw ``%VAR%`` must not remain.

        Args:
            tmp_path: Pytest-provided temporary directory.
            monkeypatch: Pytest monkeypatch fixture.
        """
        monkeypatch.setenv("VMDEPLOY_TEST_ROOT", str(tmp_path))
        var = "${VMDEPLOY_TEST_ROOT}" if os.name != "nt" else "%VMDEPLOY_TEST_ROOT%"
        config = _config(tmp_path, key_path=f"{var}/vm_key")
        assert config.ssh.key_path == tmp_path / "vm_key"

    @allure.title("An undefined environment variable is rejected, not left raw")
    def test_undefined_env_var_rejected(self, tmp_path: Path) -> None:
        """An unexpanded variable would silently point at the wrong path.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        var = "${VMDEPLOY_DOES_NOT_EXIST}" if os.name != "nt" else "%VMDEPLOY_DOES_NOT_EXIST%"
        with pytest.raises(ConfigurationError, match="unexpanded"):
            _config(tmp_path, key_path=f"{var}/vm_key")


@allure.epic("Cluster Infrastructure")
@allure.feature("Preflight")
@allure.story("Environment checks")
class TestPreflightChecks:
    """Individual checks classify a host's readiness correctly."""

    @allure.title("A missing VBoxManage is a hard failure")
    def test_missing_vboxmanage_fails(self, tmp_path: Path) -> None:
        """Without the hypervisor CLI nothing can be provisioned.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        result = check_vboxmanage(_config(tmp_path))
        assert result.status is CheckStatus.FAIL

    @allure.title("A missing SSH key is a hard failure")
    def test_missing_key_fails(self, tmp_path: Path) -> None:
        """Provisioning cannot authenticate without the key.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        result = check_ssh_key(_config(tmp_path))
        assert result.status is CheckStatus.FAIL

    @allure.title("A present SSH key passes")
    def test_present_key_passes(self, tmp_path: Path) -> None:
        """A readable key file satisfies the check.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        (tmp_path / "vm_key").write_text("key", encoding="ascii")
        result = check_ssh_key(_config(tmp_path))
        assert result.status is CheckStatus.PASS

    @allure.title("Neither an OVA nor a template VM is a hard failure")
    def test_no_image_fails(self, tmp_path: Path) -> None:
        """With no image and no template there is nothing to clone.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        result = check_template_or_ova(_config(tmp_path))
        assert result.status is CheckStatus.FAIL

    @allure.title("A present golden OVA passes the image check")
    def test_present_ova_passes(self, tmp_path: Path) -> None:
        """A built appliance is all a deploy needs.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        (tmp_path / "golden.ova").write_bytes(b"\x00" * 2048)
        result = check_template_or_ova(_config(tmp_path))
        assert result.status is CheckStatus.PASS


@allure.epic("Cluster Infrastructure")
@allure.feature("Preflight")
@allure.story("Aggregation and reporting")
class TestPreflightAggregation:
    """The suite aggregates and renders results usefully."""

    @allure.title("worst_status escalates FAIL over WARN over PASS")
    def test_worst_status_precedence(self, tmp_path: Path) -> None:
        """A bare host (no tools/keys/image) aggregates to FAIL.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        results = run_preflight(_config(tmp_path))
        assert worst_status(results) is CheckStatus.FAIL

    @allure.title("The report renders one line per check with its status")
    def test_report_format(self, tmp_path: Path) -> None:
        """Every check appears in the rendered report.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        results = run_preflight(_config(tmp_path))
        report = format_report(results)
        assert len(report.splitlines()) == len(results)
        for result in results:
            assert result.name in report
