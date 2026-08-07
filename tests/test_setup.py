"""Unit tests for credential generation and the local-overlay writer.

These cover the parts of setup that run on the automation host with no guest:
keypair generation must produce a paramiko-loadable Ed25519 key, and the
overlay writer must record the new identity where the loader will find it.
"""

from __future__ import annotations

import os
from pathlib import Path

import allure
import paramiko
import pytest

from vmdeploy.config import load_cluster_config, local_overlay_path
from vmdeploy.setup import generate_ssh_keypair, write_local_overlay


@allure.epic("Cluster Infrastructure")
@allure.feature("Security")
@allure.story("Key generation")
class TestKeyGeneration:
    """Generated keys are valid, private, and stable across reruns."""

    @allure.title("A generated keypair is a loadable Ed25519 key")
    def test_generates_loadable_key(self, tmp_path: Path) -> None:
        """Paramiko must be able to load the generated private key.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        private_path = tmp_path / "vm_key"
        public_line = generate_ssh_keypair(private_path, comment="vmadmin@vmdeploy")

        assert private_path.is_file()
        assert private_path.with_suffix(".pub").is_file()
        assert public_line.startswith("ssh-ed25519 ")
        assert public_line.endswith("vmadmin@vmdeploy")

        # The whole point: this key must work for authentication later.
        paramiko.Ed25519Key.from_private_key_file(str(private_path))

    @allure.title("The private key is not world-readable")
    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX permission bits are not enforced on Windows; ACLs govern access there",
    )
    def test_private_key_permissions(self, tmp_path: Path) -> None:
        """The private key must be created with restricted permissions.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        private_path = tmp_path / "vm_key"
        generate_ssh_keypair(private_path, comment="vmadmin@vmdeploy")
        mode = private_path.stat().st_mode & 0o077
        assert mode == 0, f"private key is group/other accessible: {oct(mode)}"

    @allure.title("Re-running setup reuses an existing keypair")
    def test_reuses_existing_key(self, tmp_path: Path) -> None:
        """A trusted key must not be silently regenerated.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        private_path = tmp_path / "vm_key"
        first = generate_ssh_keypair(private_path, comment="vmadmin@vmdeploy")
        original_bytes = private_path.read_bytes()
        second = generate_ssh_keypair(private_path, comment="vmadmin@vmdeploy")
        assert first == second
        assert private_path.read_bytes() == original_bytes


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


@allure.epic("Cluster Infrastructure")
@allure.feature("Security")
@allure.story("Local overlay")
class TestLocalOverlay:
    """The overlay keeps the real identity out of the committed base file."""

    @allure.title("The committed base carries a neutral username, not a person's")
    def test_base_has_neutral_user(self, tmp_path: Path) -> None:
        """With no overlay, the base default is used verbatim.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        base = tmp_path / "cluster.toml"
        base.write_text(_BASE.format(vboxmanage=(tmp_path / "vb").as_posix()), encoding="utf-8")
        config = load_cluster_config(base)
        assert config.ssh.user == "vmadmin"

    @allure.title("The overlay overrides the base user without editing it")
    def test_overlay_overrides_user(self, tmp_path: Path) -> None:
        """A written overlay changes the effective identity.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        base = tmp_path / "cluster.toml"
        base.write_text(_BASE.format(vboxmanage=(tmp_path / "vb").as_posix()), encoding="utf-8")

        overlay = write_local_overlay(base, "qa_engineer", tmp_path / "custom_key")
        assert overlay == local_overlay_path(base)

        config = load_cluster_config(base)
        assert config.ssh.user == "qa_engineer"
        assert config.ssh.key_path == tmp_path / "custom_key"
        # The committed base file must be untouched by the overlay write.
        assert 'user = "vmadmin"' in base.read_text(encoding="utf-8")

    @allure.title("Rewriting the overlay preserves other sections")
    def test_overlay_preserves_other_sections(self, tmp_path: Path) -> None:
        """setup must not clobber inventory/website overrides on re-run.

        Args:
            tmp_path: Pytest-provided temporary directory.
        """
        base = tmp_path / "cluster.toml"
        base.write_text(_BASE.format(vboxmanage=(tmp_path / "vb").as_posix()), encoding="utf-8")
        overlay = local_overlay_path(base)
        overlay.write_text(
            '[inventory]\nkey_file = "~/custom/inventory.key"\n'
            '[website]\nsource_url = "https://mine.example/"\n',
            encoding="utf-8",
        )

        write_local_overlay(base, "vmadmin", tmp_path / "k")

        config = load_cluster_config(base)
        assert config.ssh.user == "vmadmin"
        # Pre-existing sections must survive the rewrite.
        assert config.inventory.key_file == Path("~/custom/inventory.key").expanduser()
        assert config.website.source_url == "https://mine.example/"

    @allure.title("VMDEPLOY_SSH_USER overrides even the overlay")
    def test_env_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The environment variable has the final say for ad-hoc runs.

        Args:
            tmp_path: Pytest-provided temporary directory.
            monkeypatch: Pytest monkeypatch fixture.
        """
        base = tmp_path / "cluster.toml"
        base.write_text(_BASE.format(vboxmanage=(tmp_path / "vb").as_posix()), encoding="utf-8")
        write_local_overlay(base, "qa_engineer", tmp_path / "custom_key")

        monkeypatch.setenv("VMDEPLOY_SSH_USER", "override_user")
        config = load_cluster_config(base)
        assert config.ssh.user == "override_user"
