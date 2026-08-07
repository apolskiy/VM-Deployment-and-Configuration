"""Unit tests for guest IPv4 interface selection.

These pin the regression that failed the first live deploy: a guest cloned from
a Docker-enabled template exposes a ``docker0`` bridge at 172.17.0.1 alongside
its real NIC, and the discovery logic must never select the bridge.
"""

from __future__ import annotations

import allure
import pytest

from vmdeploy.virtualbox import _select_lan_address


@allure.epic("Cluster Infrastructure")
@allure.feature("Provisioning")
@allure.story("Guest IP selection")
class TestSelectLanAddress:
    """The reachable LAN address is chosen over virtual bridges."""

    @allure.title("docker0 is skipped in favour of the bridged NIC")
    def test_skips_docker_bridge(self) -> None:
        """The exact interface ordering from the failed deploy is handled.

        Guest Additions enumerated docker0 first (Net/0) and enp0s3 second
        (Net/1); the LAN address must still win.
        """
        interfaces = [("docker0", "172.17.0.1"), ("enp0s3", "192.168.1.64")]
        assert _select_lan_address(interfaces, "192.168.1.63") == "192.168.1.64"

    @allure.title("The interface on the host's own /24 is preferred")
    def test_prefers_host_subnet(self) -> None:
        """A same-subnet address is chosen even among several physical NICs."""
        interfaces = [("eth0", "10.0.0.5"), ("eth1", "192.168.1.64")]
        assert _select_lan_address(interfaces, "192.168.1.63") == "192.168.1.64"

    @allure.title("Without a host hint, common LAN ranges beat the Docker range")
    def test_prefers_lan_range_without_hint(self) -> None:
        """When the host LAN is unknown, 192.168/10 ranges beat 172.16/12."""
        interfaces = [("docker0", "172.17.0.1"), ("enp0s3", "192.168.1.64")]
        assert _select_lan_address(interfaces, "") == "192.168.1.64"

    @allure.title("Loopback and link-local addresses are never selected")
    def test_skips_loopback_and_link_local(self) -> None:
        """Unreachable address families are filtered out."""
        interfaces = [
            ("lo", "127.0.0.1"),
            ("enp0s3", "169.254.1.1"),
            ("enp0s8", "192.168.1.64"),
        ]
        assert _select_lan_address(interfaces, "192.168.1.63") == "192.168.1.64"

    @allure.title("Virtual bridge families are all filtered")
    @pytest.mark.parametrize(
        "virtual_name",
        ["docker0", "br-abc123", "veth9f2", "virbr0", "vboxnet0", "tun0", "flannel.1"],
    )
    def test_filters_virtual_families(self, virtual_name: str) -> None:
        """Every known virtual interface prefix is rejected.

        Args:
            virtual_name: The virtual interface name under test.
        """
        interfaces = [(virtual_name, "172.17.0.1"), ("enp0s3", "192.168.1.64")]
        assert _select_lan_address(interfaces, "") == "192.168.1.64"

    @allure.title("An all-virtual interface set yields no address")
    def test_no_physical_interface(self) -> None:
        """If only virtual interfaces exist, selection returns empty."""
        assert _select_lan_address([("docker0", "172.17.0.1")], "192.168.1.63") == ""

    @allure.title("An empty interface list yields no address")
    def test_empty(self) -> None:
        """No enumerated interfaces means nothing to select."""
        assert not _select_lan_address([], "192.168.1.63")
