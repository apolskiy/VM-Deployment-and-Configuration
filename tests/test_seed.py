"""Unit tests for the cloud-init NoCloud seed builder.

These pin the two things a seed gets wrong silently. First, the ISO layout:
cloud-init finds the datasource only if the volume is labelled ``cidata`` and
holds lowercase ``user-data`` and ``meta-data`` at the root, and a guest that
misses it boots with no identity and no way in. Second, input validation: a
malformed key or hostname produces a VM that boots fine and then refuses every
login, which is expensive to diagnose against a headless guest and cheap to
reject here.

Everything runs offline against a real ISO on disk — no hypervisor involved.
"""

from __future__ import annotations

import io
from pathlib import Path

import allure
import pycdlib
import pytest

from vmdeploy.exceptions import VmDeployError
from vmdeploy.seed import (
    SEED_VOLUME_LABEL,
    build_seed_iso,
    render_meta_data,
    render_user_data,
)

_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJz0Q8example vmadmin@host"


def _read_iso_file(iso_path: Path, name: str) -> str:
    """Read one file back out of a seed ISO by its Joliet name.

    Args:
        iso_path: The ISO to open.
        name: The lowercase file name, as cloud-init would look for it.

    Returns:
        The file's decoded contents.
    """
    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    try:
        buffer = io.BytesIO()
        iso.get_file_from_iso_fp(buffer, joliet_path=f"/{name}")
        return buffer.getvalue().decode("utf-8")
    finally:
        iso.close()


@allure.epic("Cluster Infrastructure")
@allure.feature("Provisioning")
@allure.story("Cloud-init seed medium")
class TestSeedIsoLayout:
    """The ISO carries exactly what cloud-init's NoCloud datasource looks for."""

    @allure.title("The volume is labelled cidata")
    def test_volume_label(self, tmp_path: Path) -> None:
        """A different label means the datasource is never found."""
        iso_path = build_seed_iso(tmp_path / "seed.iso", "apjump", "vmadmin", _KEY)
        iso = pycdlib.PyCdlib()
        iso.open(str(iso_path))
        try:
            label = iso.pvd.volume_identifier.decode("utf-8").strip()
        finally:
            iso.close()
        assert label == SEED_VOLUME_LABEL

    @allure.title("Both documents are present under their lowercase names")
    def test_documents_present(self, tmp_path: Path) -> None:
        """Plain ISO 9660 would truncate these to 8.3 uppercase."""
        iso_path = build_seed_iso(tmp_path / "seed.iso", "apnode1", "vmadmin", _KEY)
        assert "#cloud-config" in _read_iso_file(iso_path, "user-data")
        assert "instance-id:" in _read_iso_file(iso_path, "meta-data")

    @allure.title("The deployer's key reaches the guest")
    def test_key_is_carried(self, tmp_path: Path) -> None:
        """The injected key is the only thing that will be able to log in."""
        iso_path = build_seed_iso(tmp_path / "seed.iso", "apjump", "vmadmin", _KEY)
        assert _KEY in _read_iso_file(iso_path, "user-data")

    @allure.title("An existing ISO is replaced, not appended to")
    def test_rewrite_is_clean(self, tmp_path: Path) -> None:
        """Re-provisioning a guest must not inherit the previous identity."""
        destination = tmp_path / "seed.iso"
        build_seed_iso(destination, "apnode1", "vmadmin", _KEY)
        build_seed_iso(destination, "apnode2", "vmadmin", _KEY)
        meta = _read_iso_file(destination, "meta-data")
        assert "apnode2" in meta
        assert "apnode1" not in meta

    @allure.title("Each guest gets a unique instance id")
    def test_instance_ids_differ(self, tmp_path: Path) -> None:
        """cloud-init reruns per-instance modules only when the id changes."""
        first = _read_iso_file(build_seed_iso(tmp_path / "a.iso", "apjump", "vmadmin", _KEY),
                               "meta-data")
        second = _read_iso_file(build_seed_iso(tmp_path / "b.iso", "apjump", "vmadmin", _KEY),
                                "meta-data")
        assert first != second

    @allure.title("An explicit instance id is honoured")
    def test_explicit_instance_id(self, tmp_path: Path) -> None:
        """Callers that need a reproducible seed can pin the id."""
        iso_path = build_seed_iso(
            tmp_path / "seed.iso", "apjump", "vmadmin", _KEY, instance_id="fixed-001"
        )
        assert "fixed-001" in _read_iso_file(iso_path, "meta-data")


@allure.epic("Cluster Infrastructure")
@allure.feature("Provisioning")
@allure.story("Cloud-init seed content")
class TestUserData:
    """The cloud-config creates one key-only account and no stock user."""

    @allure.title("Only the named account is created")
    def test_no_default_user(self) -> None:
        """Omitting 'default' is what stops cloud-init adding the distro user."""
        document = render_user_data("apjump", "vmadmin", _KEY)
        assert "name: 'vmadmin'" in document
        assert "default" not in document

    @allure.title("Password login is disabled outright")
    def test_password_login_disabled(self) -> None:
        """A published image must not be reachable by password."""
        document = render_user_data("apjump", "vmadmin", _KEY)
        assert "lock_passwd: true" in document
        assert "ssh_pwauth: false" in document
        assert "disable_root: true" in document

    @allure.title("The document declares itself as cloud-config")
    def test_cloud_config_header(self) -> None:
        """Without the header line cloud-init ignores the payload entirely."""
        assert render_user_data("apjump", "vmadmin", _KEY).startswith("#cloud-config\n")


@allure.epic("Cluster Infrastructure")
@allure.feature("Provisioning")
@allure.story("Cloud-init seed validation")
class TestValidation:
    """Unusable input is rejected before an ISO exists."""

    @allure.title("A private key is rejected as the deployer key")
    def test_private_key_rejected(self) -> None:
        """Passing the private key by mistake would leak it into the seed."""
        with pytest.raises(VmDeployError, match="unrecognised public key type"):
            render_user_data("apjump", "vmadmin", "-----BEGIN OPENSSH PRIVATE KEY-----")

    @allure.title("An empty key is rejected")
    def test_empty_key_rejected(self) -> None:
        """An empty key yields a guest nothing can log in to."""
        with pytest.raises(VmDeployError, match="empty"):
            render_user_data("apjump", "vmadmin", "   ")

    @allure.title("A multi-line key is rejected")
    def test_multiline_key_rejected(self) -> None:
        """Two keys in one field would break the YAML list item."""
        with pytest.raises(VmDeployError, match="multiple lines"):
            render_user_data("apjump", "vmadmin", f"{_KEY}\n{_KEY}")

    @pytest.mark.parametrize(
        "hostname",
        ["-apjump", "apjump-", "ap jump", "ap_jump", "", "a" * 64],
        ids=["leading-hyphen", "trailing-hyphen", "space", "underscore", "empty", "too-long"],
    )
    @allure.title("Invalid hostname is rejected: {hostname}")
    def test_invalid_hostname_rejected(self, hostname: str) -> None:
        """cloud-init would either fail or apply a mangled name.

        Args:
            hostname: The candidate hostname under test.
        """
        with pytest.raises(VmDeployError, match="invalid hostname"):
            render_meta_data(hostname, "id-1")

    @allure.title("Invalid username is rejected")
    def test_invalid_username_rejected(self) -> None:
        """useradd would refuse the name after the guest had already booted."""
        with pytest.raises(VmDeployError, match="invalid username"):
            render_user_data("apjump", "Vm Admin", _KEY)

    @allure.title("A quote in a value cannot break out of the YAML scalar")
    def test_yaml_quoting_is_escaped(self) -> None:
        """Single-quoted YAML escapes a quote by doubling it."""
        document = render_meta_data("apjump", "id-'injected")
        assert "'id-''injected'" in document
