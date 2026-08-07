"""Unit tests for optical drive slot discovery.

The seed ISO is attached to a slot chosen from whatever the imported appliance
happens to expose. Choosing wrongly is not a soft failure: attaching over the
SATA slot would detach the guest's root disk and the machine would never boot,
so the selection rules are pinned here against real VBoxManage output.
"""

from __future__ import annotations

import sys
from pathlib import Path

import allure
import pytest

from vmdeploy.exceptions import VirtualBoxError
from vmdeploy.virtualbox import ProcessResult, VBoxManage

# Trimmed from `VBoxManage showvminfo apnode1 --machinereadable` on a guest
# imported from the project's golden OVA.
_REAL_LAYOUT = """\
storagecontrollername0="IDE"
storagecontrollertype0="PIIX4"
storagecontrollername1="SATA"
storagecontrollertype1="IntelAhci"
"IDE-0-0"="emptydrive"
"IDE-IsEjected-0-0"="off"
"IDE-0-1"="none"
"IDE-1-0"="none"
"IDE-1-1"="none"
"SATA-0-0"="C:\\\\Users\\\\vorns\\\\VirtualBox VMs\\\\apnode1\\\\apubuntuD-disk001.vmdk"
"SATA-ImageUUID-0-0"="493937bb-4310-4064-b674-b8e803773f92"
"""

_NO_OPTICAL = """\
storagecontrollername0="SATA"
storagecontrollertype0="IntelAhci"
"SATA-0-0"="/vms/disk.vmdk"
"""

_SEED_ALREADY_IN = """\
storagecontrollername0="IDE"
storagecontrollertype0="PIIX4"
"IDE-0-0"="C:\\\\Users\\\\vorns\\\\templates\\\\seeds\\\\apjump-seed.iso"
"SATA-0-0"="/vms/disk.vmdk"
storagecontrollername1="SATA"
"""


class _FakeVBox(VBoxManage):
    """A VBoxManage whose showvminfo output is scripted."""

    def __init__(self, stdout: str) -> None:
        """Bind to a real file so the base constructor passes, then script output.

        Args:
            stdout: The showvminfo output to return.
        """
        super().__init__(Path(sys.executable))
        self._stdout = stdout
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, check: bool = True, timeout: int = 120) -> ProcessResult:
        """Record the invocation and return the scripted output.

        Args:
            *args: The VBoxManage arguments.
            check: Ignored.
            timeout: Ignored.

        Returns:
            The scripted process result.
        """
        self.calls.append(args)
        return ProcessResult(args_used=args, exit_status=0, stdout=self._stdout, stderr="")


@allure.epic("Cluster Infrastructure")
@allure.feature("Provisioning")
@allure.story("Seed medium attachment")
class TestOpticalSlot:
    """The slot chosen must be an optical one, never a disk."""

    @allure.title("The appliance's empty DVD drive is selected")
    def test_selects_empty_drive(self) -> None:
        """IDE-0-0 is the drive the golden OVA ships."""
        assert _FakeVBox(_REAL_LAYOUT).optical_slot("apnode1") == ("IDE", 0, 0)

    @allure.title("The root disk is never selected")
    def test_never_selects_disk(self) -> None:
        """Attaching over SATA-0-0 would detach the guest's root filesystem."""
        controller, _, _ = _FakeVBox(_REAL_LAYOUT).optical_slot("apnode1")
        assert controller != "SATA"

    @allure.title("A slot already holding a seed is reused")
    def test_reuses_slot_with_iso(self) -> None:
        """Re-provisioning must replace the seed, not look for another slot."""
        assert _FakeVBox(_SEED_ALREADY_IN).optical_slot("apjump") == ("IDE", 0, 0)

    @allure.title("A machine with no optical slot is refused")
    def test_no_optical_slot_raises(self) -> None:
        """Silently doing nothing would yield a guest with no identity."""
        with pytest.raises(VirtualBoxError, match="no optical drive slot"):
            _FakeVBox(_NO_OPTICAL).optical_slot("apnode1")


@allure.epic("Cluster Infrastructure")
@allure.feature("Provisioning")
@allure.story("Seed medium attachment")
class TestAttachDetach:
    """Attach and eject target the discovered slot."""

    @allure.title("Attaching passes the ISO to the discovered slot")
    def test_attach_targets_slot(self) -> None:
        """The medium must land in the optical drive, as a dvddrive."""
        vbox = _FakeVBox(_REAL_LAYOUT)
        vbox.attach_optical("apnode1", Path("C:/templates/seeds/apnode1-seed.iso"))
        attach = vbox.calls[-1]
        assert attach[0] == "storageattach"
        assert "--storagectl" in attach and "IDE" in attach
        assert "--type" in attach and "dvddrive" in attach
        assert attach[-1].endswith("apnode1-seed.iso")

    @allure.title("Ejecting leaves an empty drive behind")
    def test_detach_empties_drive(self) -> None:
        """Leaving a stale medium registered would block a later import."""
        vbox = _FakeVBox(_REAL_LAYOUT)
        vbox.detach_optical("apnode1")
        assert vbox.calls[-1][-1] == "emptydrive"

    @allure.title("Ejecting from a machine with no optical slot is tolerated")
    def test_detach_without_slot_is_quiet(self) -> None:
        """Teardown must not fail on a guest that never had a seed."""
        vbox = _FakeVBox(_NO_OPTICAL)
        vbox.detach_optical("apnode1")
        assert all(call[0] != "storageattach" for call in vbox.calls)
