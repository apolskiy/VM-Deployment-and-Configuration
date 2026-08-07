"""Unit tests for snapshot detection parsing.

The snapshot take/restore/delete calls are thin VBoxManage wrappers exercised
by integration; the one piece worth pinning in isolation is how a snapshot's
presence is detected from ``snapshot list`` output, since a wrong answer there
would either skip a needed snapshot or fail a restore.
"""

from __future__ import annotations

import sys
from pathlib import Path

import allure

from vmdeploy.virtualbox import ProcessResult, VBoxManage


class _FakeVBox(VBoxManage):
    """A VBoxManage whose command output is scripted."""

    def __init__(self, stdout: str, ok: bool = True) -> None:
        """Bind to a real file so the base constructor passes, then script output.

        Args:
            stdout: The stdout to return from run().
            ok: Whether the scripted invocation succeeds.
        """
        super().__init__(Path(sys.executable))
        self._stdout = stdout
        self._ok = ok

    def run(self, *args: str, check: bool = True, timeout: int = 120) -> ProcessResult:
        """Return the scripted result regardless of arguments.

        Args:
            *args: Ignored.
            check: Ignored.
            timeout: Ignored.

        Returns:
            The scripted process result.
        """
        return ProcessResult(
            args_used=args, exit_status=0 if self._ok else 1, stdout=self._stdout, stderr=""
        )


@allure.epic("Cluster Infrastructure")
@allure.feature("Provisioning")
@allure.story("Snapshot detection")
class TestHasSnapshot:
    """A named snapshot is detected only when actually present."""

    @allure.title("A present snapshot is detected")
    def test_present(self) -> None:
        """The snapshot name appears in machinereadable output."""
        vbox = _FakeVBox('SnapshotName="vmdeploy-prebake"\nSnapshotUUID="{abc}"\n')
        assert vbox.has_snapshot("apubuntuD", "vmdeploy-prebake") is True

    @allure.title("A different snapshot name is not a false match")
    def test_absent_among_others(self) -> None:
        """An unrelated snapshot must not be mistaken for the wanted one."""
        vbox = _FakeVBox('SnapshotName="something-else"\n')
        assert vbox.has_snapshot("apubuntuD", "vmdeploy-prebake") is False

    @allure.title("No snapshots means not present")
    def test_none(self) -> None:
        """Empty output means the machine has no snapshots."""
        vbox = _FakeVBox("", ok=False)
        assert vbox.has_snapshot("apubuntuD", "vmdeploy-prebake") is False
