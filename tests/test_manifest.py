"""Unit tests for the canonical local encrypted manifest.

The manifest is the durable record of cluster state. Deploy writes it with hosts
Active; teardown must rewrite it with hosts Removed, so it never silently goes
stale after the jump station that serves the live copy is destroyed.
"""

from __future__ import annotations

from pathlib import Path

import allure
import pytest

from vmdeploy.config import ClusterConfig, load_cluster_config
from vmdeploy.inventory import decrypt_records, load_key
from vmdeploy.inventory_service import (
    build_records,
    mark_manifest_removed,
    read_local_manifest,
    write_local_manifest,
)

_TEMPLATE = """
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


def _config(tmp_path: Path) -> ClusterConfig:
    """Build a cluster config whose key and manifest live under tmp_path.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        The loaded configuration.
    """
    key_file = (tmp_path / "inventory.key").as_posix()
    manifest_file = (tmp_path / "inventory.enc").as_posix()
    text = _TEMPLATE.format(key_file=key_file, manifest_file=manifest_file)
    path = tmp_path / "cluster.toml"
    path.write_text(text, encoding="utf-8")
    return load_cluster_config(path)


@allure.epic("Cluster Infrastructure")
@allure.feature("Encrypted Inventory")
@allure.story("Local manifest lifecycle")
@pytest.mark.inventory
class TestManifestLifecycle:
    """Deploy and teardown both keep the local manifest current."""

    @allure.title("A deployed manifest round-trips through encryption")
    def test_write_then_read(self, tmp_path: Path) -> None:
        """Records written to the manifest read back identical and encrypted.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        config = _config(tmp_path)
        records = build_records(
            config, {"apjump": "192.168.1.66", "apnode1": "192.168.1.67", "apnode2": ""}
        )
        write_local_manifest(config, records)

        with allure.step("Manifest file is genuine ciphertext"):
            raw = config.inventory.manifest_file.read_bytes()
            assert b"apjump" not in raw

        assert read_local_manifest(config) == records

    @allure.title("Reading an absent manifest yields no records")
    def test_read_absent(self, tmp_path: Path) -> None:
        """A missing manifest is the legitimate pre-deploy state.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        assert not read_local_manifest(_config(tmp_path))

    @allure.title("Teardown flips every host to Removed/Inactive")
    def test_mark_removed_preserves_last_known(self, tmp_path: Path) -> None:
        """Teardown records removal while keeping last-known addresses.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        config = _config(tmp_path)
        deployed = build_records(
            config,
            {"apjump": "192.168.1.66", "apnode1": "192.168.1.67", "apnode2": "192.168.1.68"},
        )
        write_local_manifest(config, deployed)

        removed = mark_manifest_removed(config)

        assert {record.hostname for record in removed} == {"apjump", "apnode1", "apnode2"}
        for record in removed:
            assert record.status == "Removed"
            assert record.state == "Inactive"
        # Last-known address is preserved for the audit trail.
        by_host = {record.hostname: record for record in removed}
        assert by_host["apnode1"].ipv4 == "192.168.1.67"

        with allure.step("The change is persisted, not just returned"):
            assert read_local_manifest(config) == removed

    @allure.title("Teardown with no prior manifest still records all hosts removed")
    def test_mark_removed_without_prior_manifest(self, tmp_path: Path) -> None:
        """A teardown before any successful deploy still writes a complete record.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        config = _config(tmp_path)
        removed = mark_manifest_removed(config)

        assert {record.hostname for record in removed} == {"apjump", "apnode1", "apnode2"}
        assert all(record.status == "Removed" for record in removed)

    @allure.title("The manifest is decryptable with the deployment key")
    def test_manifest_uses_deployment_key(self, tmp_path: Path) -> None:
        """The manifest is bound to the same key the service uses.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        config = _config(tmp_path)
        records = build_records(config, {"apjump": "192.168.1.66"})
        write_local_manifest(config, records)

        key = load_key(config.inventory.key_file)
        raw = config.inventory.manifest_file.read_bytes()
        assert decrypt_records(raw, key) == records
