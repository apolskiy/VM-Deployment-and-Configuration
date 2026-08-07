"""Regression tests for verified VM deletion.

A teardown once left a stray VM behind: ``unregistervm --delete`` was issued
once, unchecked, and failed silently on a transient VirtualBox lock. These
tests pin the fix — delete is retried and verified, and a VM that never
disappears raises rather than passing silently.
"""

from __future__ import annotations

import sys
from pathlib import Path

import allure
import pytest

from vmdeploy.exceptions import VirtualBoxError
from vmdeploy.virtualbox import ProcessResult, VBoxManage, VMState


class _FakeVBox(VBoxManage):
    """A VBoxManage whose registry and command results are scripted.

    Attributes:
        exists_sequence: Values returned by successive ``exists`` calls.
        delete_calls: Number of ``unregistervm --delete`` invocations made.
    """

    def __init__(self, exists_sequence: list[bool]) -> None:
        """Bind to a real file so the base constructor passes, then script state.

        Args:
            exists_sequence: Values ``exists`` returns on successive calls.
        """
        super().__init__(Path(sys.executable))
        self.exists_sequence = exists_sequence
        self.delete_calls = 0

    def exists(self, vm_name: str) -> bool:
        """Return the next scripted existence value.

        Args:
            vm_name: Ignored.

        Returns:
            The next value from the scripted sequence, or its last value.
        """
        if len(self.exists_sequence) > 1:
            return self.exists_sequence.pop(0)
        return self.exists_sequence[0]

    def power_off(self, vm_name: str, *, wait_seconds: int = 120) -> None:
        """No-op power off.

        Args:
            vm_name: Ignored.
            wait_seconds: Ignored.
        """

    def state(self, vm_name: str) -> VMState:
        """Report the machine as powered off.

        Args:
            vm_name: Ignored.

        Returns:
            Always :attr:`VMState.POWERED_OFF`.
        """
        return VMState.POWERED_OFF

    def run(self, *args: str, check: bool = True, timeout: int = 120) -> ProcessResult:
        """Count delete calls; succeed for everything.

        Args:
            *args: The VBoxManage argument vector.
            check: Ignored.
            timeout: Ignored.

        Returns:
            A successful process result.
        """
        if "unregistervm" in args:
            self.delete_calls += 1
        return ProcessResult(args_used=args, exit_status=0, stdout="", stderr="")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the inter-attempt delay so retries run instantly.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr("vmdeploy.virtualbox.time.sleep", lambda _seconds: None)


@allure.epic("Cluster Infrastructure")
@allure.feature("Provisioning")
@allure.story("Verified VM deletion")
class TestDestroyRetry:
    """Deletion is verified, retried on transient failure, and raises if stuck."""

    @allure.title("A VM that deletes on the first try needs one attempt")
    def test_deletes_first_try(self) -> None:
        """The happy path issues exactly one delete."""
        vbox = _FakeVBox(exists_sequence=[True, False])
        vbox.destroy("apnode1")
        assert vbox.delete_calls == 1

    @allure.title("A transient lock is retried until the VM is gone")
    def test_retries_transient_lock(self) -> None:
        """A VM still present after the first delete is retried and verified."""
        # Present initially, still present after attempt 1, gone after attempt 2.
        vbox = _FakeVBox(exists_sequence=[True, True, False])
        vbox.destroy("apnode1", attempts=3)
        assert vbox.delete_calls == 2

    @allure.title("A VM that never disappears raises instead of passing silently")
    def test_raises_when_stuck(self) -> None:
        """The exact failure that left a stray VM must now surface loudly."""
        vbox = _FakeVBox(exists_sequence=[True])
        with pytest.raises(VirtualBoxError, match="could not be deleted"):
            vbox.destroy("apnode1", attempts=3)
        assert vbox.delete_calls == 3

    @allure.title("An absent VM is a no-op")
    def test_absent_is_noop(self) -> None:
        """Destroying an unregistered VM issues no delete."""
        vbox = _FakeVBox(exists_sequence=[False])
        vbox.destroy("apnode1")
        assert vbox.delete_calls == 0
