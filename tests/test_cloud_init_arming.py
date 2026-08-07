"""Unit tests for arming cloud-init in the golden image.

These pin the failure modes that a live guest hides until it is too late. A
guest cloned from a mis-armed image boots normally and is then unreachable,
with nothing on the console to say why, so each precondition is asserted here
instead of discovered against a headless VM.

The ordering test is the important one. ``cloud-init clean`` executes the hooks
in /etc/cloud/clean.d, which delete drop-in configuration; writing the drop-ins
before the clean therefore silently loses them. That is a real defect this
suite exists to prevent recurring.
"""

from __future__ import annotations

import pytest

import allure

from vmdeploy.exceptions import VmDeployError
from vmdeploy.setup import arm_cloud_init
from vmdeploy.ssh_client import CommandResult

_DATASOURCE_DROPIN = "/etc/cloud/cloud.cfg.d/99-vmdeploy-nocloud.cfg"
_NETWORK_DROPIN = "/etc/cloud/cloud.cfg.d/99-vmdeploy-network.cfg"


class _FakeHost:
    """A RemoteHost stand-in that records commands and serves file reads.

    The check and timeout parameters are unused but must be accepted: this
    stands in for RemoteHost, and dropping them would let a signature drift in
    the real client pass unnoticed here.

    Attributes:
        address: The host label used in log messages.
        commands: Every command issued, in order, tagged run or sudo.
        installed: Remote paths installed from staged uploads.
    """

    # pylint: disable=unused-argument

    def __init__(self, *, surviving_flags: str = "") -> None:
        """Set up an armed-cleanly host unless leftovers are requested.

        Args:
            surviving_flags: Output to report from the leftover-opt-out probe,
                simulating an installer file that could not be removed.
        """
        self.address = "test-host"
        self.commands: list[tuple[str, str]] = []
        self.installed: dict[str, str] = {}
        self._staged: dict[str, str] = {}
        self._surviving_flags = surviving_flags

    def run(self, command: str, *, check: bool = True, timeout: int | None = None) -> CommandResult:
        """Record a command and answer the leftover probe.

        Args:
            command: The command issued.
            check: Ignored.
            timeout: Ignored.

        Returns:
            A successful result, carrying leftover output where relevant.
        """
        self.commands.append(("run", command))
        stdout = self._surviving_flags if "cloud-init.disabled" in command else ""
        return CommandResult(command=command, exit_status=0, stdout=stdout, stderr="")

    def sudo(
        self, command: str, *, check: bool = True, timeout: int | None = None
    ) -> CommandResult:
        """Record a privileged command, emulating install and cat.

        Args:
            command: The command issued.
            check: Ignored.
            timeout: Ignored.

        Returns:
            A successful result, carrying file contents for reads.
        """
        self.commands.append(("sudo", command))
        if command.startswith("install "):
            source, destination = command.split()[-2:]
            self.installed[destination] = self._staged.get(source, "")
        stdout = ""
        if command.startswith("cat "):
            stdout = self.installed.get(command.split()[1], "")
        return CommandResult(command=command, exit_status=0, stdout=stdout, stderr="")

    def put_bytes(self, payload: bytes, remote_path: str, *, mode: int = 0o644) -> None:
        """Stage an upload so a later install can pick it up.

        Args:
            payload: The bytes uploaded.
            remote_path: Where they were staged.
            mode: Ignored.
        """
        self._staged[remote_path] = payload.decode("utf-8")

    def index_of(self, needle: str) -> int:
        """Return the position of the first command containing a substring.

        Args:
            needle: The substring to find.

        Returns:
            The index in the recorded command list.

        Raises:
            AssertionError: If no command matched.
        """
        for position, (_, command) in enumerate(self.commands):
            if needle in command:
                return position
        raise AssertionError(f"no command contained {needle!r}: {self.commands}")


@allure.epic("Cluster Infrastructure")
@allure.feature("Provisioning")
@allure.story("Cloud-init arming")
class TestArmingOrder:
    """Drop-ins must outlive the clean that removes installer configuration."""

    @allure.title("Drop-ins are written after cloud-init clean, not before")
    def test_dropins_written_after_clean(self) -> None:
        """clean runs /etc/cloud/clean.d hooks, which delete drop-in files."""
        host = _FakeHost()
        arm_cloud_init(host)  # type: ignore[arg-type]
        clean_at = host.index_of("cloud-init clean")
        assert host.index_of(_DATASOURCE_DROPIN) > clean_at
        assert host.index_of(_NETWORK_DROPIN) > clean_at

    @allure.title("The installer opt-outs are removed before the clean")
    def test_optouts_removed_first(self) -> None:
        """Removing them first keeps the clean from re-reading stale settings."""
        host = _FakeHost()
        arm_cloud_init(host)  # type: ignore[arg-type]
        assert host.index_of("rm -f /etc/cloud/cloud-init.disabled") < host.index_of(
            "cloud-init clean"
        )

    @allure.title("machine-id is reset so each clone regenerates its own")
    def test_machine_id_reset(self) -> None:
        """Clones sharing a machine-id would collide on DHCP identity."""
        host = _FakeHost()
        arm_cloud_init(host)  # type: ignore[arg-type]
        assert "--machine-id" in host.commands[host.index_of("cloud-init clean")][1]


@allure.epic("Cluster Infrastructure")
@allure.feature("Provisioning")
@allure.story("Cloud-init arming")
class TestArmingContent:
    """What is written must actually select NoCloud and leave the network alone."""

    @allure.title("NoCloud is probed first, with None as fallback")
    def test_datasource_selects_nocloud(self) -> None:
        """Without this the seed ISO is never looked for."""
        host = _FakeHost()
        arm_cloud_init(host)  # type: ignore[arg-type]
        assert "datasource_list: [ NoCloud, None ]" in host.installed[_DATASOURCE_DROPIN]

    @allure.title("cloud-init network management stays disabled")
    def test_network_left_alone(self) -> None:
        """The installer's own drop-in saying this is deleted by clean."""
        host = _FakeHost()
        arm_cloud_init(host)  # type: ignore[arg-type]
        assert "network: {config: disabled}" in host.installed[_NETWORK_DROPIN]


@allure.epic("Cluster Infrastructure")
@allure.feature("Provisioning")
@allure.story("Cloud-init arming")
class TestArmingVerification:
    """A mis-armed image is refused before it can be exported."""

    @allure.title("A surviving installer opt-out fails the build")
    def test_surviving_optout_raises(self) -> None:
        """Exporting this would yield clones with no identity and no way in."""
        host = _FakeHost(surviving_flags="/etc/cloud/cloud-init.disabled")
        with pytest.raises(VmDeployError, match="still disabled"):
            arm_cloud_init(host)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("dropin", "expected"),
        [(_DATASOURCE_DROPIN, "would not probe"), (_NETWORK_DROPIN, "regenerate netplan")],
        ids=["datasource", "network"],
    )
    @allure.title("A missing drop-in fails the build: {dropin}")
    def test_missing_dropin_raises(self, dropin: str, expected: str) -> None:
        """Each drop-in is separately load-bearing, so each is checked.

        Args:
            dropin: The drop-in to suppress.
            expected: Text the resulting diagnosis must contain.
        """

        class _LosesDropin(_FakeHost):
            """A host on which one specific drop-in fails to persist."""

            def sudo(
                self, command: str, *, check: bool = True, timeout: int | None = None
            ) -> CommandResult:
                """Drop the targeted install, then behave normally.

                Args:
                    command: The command issued.
                    check: Ignored.
                    timeout: Ignored.

                Returns:
                    The result from the base fake.
                """
                if command.startswith("install ") and command.endswith(dropin):
                    self.commands.append(("sudo", command))
                    return CommandResult(command=command, exit_status=0, stdout="", stderr="")
                return super().sudo(command, check=check, timeout=timeout)

        with pytest.raises(VmDeployError, match=expected):
            arm_cloud_init(_LosesDropin())  # type: ignore[arg-type]
