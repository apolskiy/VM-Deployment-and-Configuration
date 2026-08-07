"""Pre-deployment resource and environment checks.

Provisioning boots several virtual machines and exports a multi-gigabyte
appliance, so it fails slowly and confusingly when the host is short on RAM or
disk, or when a required tool or key is missing. This module answers one
question up front: *is this host ready to deploy or tear down the configured
cluster?* Run it before either operation.

Checks degrade gracefully: a check whose inputs cannot be read is reported as a
warning rather than crashing the run, so preflight itself never becomes the
thing that blocks progress.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

from vmdeploy.config import ClusterConfig
from vmdeploy.virtualbox import VBoxManage

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

# Headroom kept free for the host OS and the automation process itself, on top
# of what the guests reserve.
_HOST_RAM_HEADROOM_MB: Final[int] = 1024
# Each imported guest needs roughly the appliance's own footprint of disk; this
# multiplier adds slack for growth and snapshots.
_DISK_SLACK: Final[float] = 1.3
_MB: Final[int] = 1024 * 1024
_GB: Final[float] = 1024.0 * _MB


class CheckStatus(Enum):
    """The outcome of a single preflight check.

    Attributes:
        PASS: The requirement is satisfied.
        WARN: A concern worth surfacing that does not block the operation.
        FAIL: A hard blocker; the operation should not proceed.
    """

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The result of one named preflight check.

    Attributes:
        name: Short identifier for the check.
        status: Whether the check passed, warned, or failed.
        detail: Human-readable explanation, including measured values.
    """

    name: str
    status: CheckStatus
    detail: str


def _host_available_ram_mb() -> int | None:
    """Return the host's currently available RAM in megabytes.

    Returns:
        Available RAM in megabytes, or None if it cannot be determined on this
        platform.
    """
    if platform.system() == "Windows":
        return _windows_available_ram_mb()
    # os.sysconf exists only on POSIX; getattr keeps this importable and
    # type-checkable on Windows, where the branch above is taken anyway.
    sysconf = getattr(os, "sysconf", None)
    if not callable(sysconf):
        return None
    try:
        # pylint: disable-next=not-callable
        return int(sysconf("SC_AVPHYS_PAGES") * sysconf("SC_PAGE_SIZE") / _MB)
    except (ValueError, OSError):
        return None


class _MemoryStatusEx(ctypes.Structure):
    """Mirror of the Win32 ``MEMORYSTATUSEX`` structure.

    Field names must match the Win32 API exactly, so they do not follow the
    project's snake_case convention.
    """

    # pylint: disable=too-few-public-methods
    # DWORD is a 32-bit unsigned int; c_uint32 avoids importing ctypes.wintypes,
    # which does not exist on non-Windows platforms.
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _windows_available_ram_mb() -> int | None:
    """Return available physical RAM on Windows via GlobalMemoryStatusEx.

    Returns:
        Available RAM in megabytes, or None if the call fails.
    """
    # ctypes.windll exists only on Windows. Reaching it via getattr keeps this
    # module type-checkable on Linux (CI), where a static ctypes.windll access
    # is an error, without needing a platform-specific type: ignore that would
    # itself be flagged as unused on Windows.
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None

    status = _MemoryStatusEx()
    # dwLength mirrors the Win32 field name and is set as the API requires.
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)  # pylint: disable=invalid-name,attribute-defined-outside-init
    if not windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullAvailPhys / _MB)


def _vbox_machine_folder(vbox: VBoxManage) -> Path | None:
    """Return the VirtualBox default machine folder, where guests are stored.

    Args:
        vbox: The hypervisor wrapper.

    Returns:
        The default machine folder, or None if it cannot be read.
    """
    result = vbox.run("list", "systemproperties", check=False)
    if not result.ok:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("Default machine folder:"):
            return Path(line.split(":", 1)[1].strip())
    return None


def check_vboxmanage(config: ClusterConfig) -> CheckResult:
    """Check that the VBoxManage executable exists and runs.

    Args:
        config: The cluster configuration.

    Returns:
        The check result.
    """
    path = config.virtualbox.vboxmanage
    if not path.is_file():
        return CheckResult(
            "VBoxManage",
            CheckStatus.FAIL,
            f"not found at {path}; set [virtualbox].vboxmanage in the configuration",
        )
    try:
        version = VBoxManage(path).version()
    except (OSError, ValueError) as exc:
        return CheckResult("VBoxManage", CheckStatus.FAIL, f"found but not runnable: {exc}")
    return CheckResult("VBoxManage", CheckStatus.PASS, f"VirtualBox {version} at {path}")


def check_ssh_key(config: ClusterConfig) -> CheckResult:
    """Check that the configured SSH private key is present.

    Args:
        config: The cluster configuration.

    Returns:
        The check result.
    """
    path = config.ssh.key_path
    if not path.is_file():
        return CheckResult(
            "SSH key",
            CheckStatus.FAIL,
            f"private key not found at {path}; set [ssh].key_path",
        )
    return CheckResult("SSH key", CheckStatus.PASS, f"present at {path}")


def check_key_directory_writable(config: ClusterConfig) -> CheckResult:
    """Check that the inventory key and manifest directory can be written.

    Args:
        config: The cluster configuration.

    Returns:
        The check result.
    """
    target = config.inventory.manifest_file.parent
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".vmdeploy-write-test"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            "Key/manifest dir", CheckStatus.FAIL, f"{target} is not writable: {exc}"
        )
    return CheckResult("Key/manifest dir", CheckStatus.PASS, f"{target} is writable")


def check_host_ram(config: ClusterConfig) -> CheckResult:
    """Check the host has enough free RAM for the configured guests.

    Args:
        config: The cluster configuration.

    Returns:
        The check result.
    """
    guests = len(config.all_hosts)
    required = guests * config.virtualbox.memory_mb + _HOST_RAM_HEADROOM_MB
    available = _host_available_ram_mb()
    if available is None:
        return CheckResult(
            "Host RAM", CheckStatus.WARN, "could not determine available RAM on this platform"
        )

    summary = (
        f"{available} MB available, need ~{required} MB "
        f"({guests} guest(s) x {config.virtualbox.memory_mb} MB + "
        f"{_HOST_RAM_HEADROOM_MB} MB headroom)"
    )
    if available >= required:
        return CheckResult("Host RAM", CheckStatus.PASS, summary)
    if available >= guests * config.virtualbox.memory_mb:
        return CheckResult(
            "Host RAM",
            CheckStatus.WARN,
            summary + "; guests fit but headroom is tight, expect paging",
        )
    return CheckResult("Host RAM", CheckStatus.FAIL, summary)


def check_host_disk(config: ClusterConfig) -> CheckResult:
    """Check the guest storage drive has enough free disk.

    Args:
        config: The cluster configuration.

    Returns:
        The check result.
    """
    vbox_path = config.virtualbox.vboxmanage
    machine_folder: Path | None = None
    if vbox_path.is_file():
        machine_folder = _vbox_machine_folder(VBoxManage(vbox_path))
    probe_dir = machine_folder or config.virtualbox.template_ova.parent

    try:
        usage = shutil.disk_usage(probe_dir if probe_dir.exists() else probe_dir.anchor)
    except OSError as exc:
        return CheckResult("Host disk", CheckStatus.WARN, f"could not measure {probe_dir}: {exc}")

    ova = config.virtualbox.template_ova
    per_guest_gb = ova.stat().st_size / _GB if ova.is_file() else 8.0
    required_gb = per_guest_gb * len(config.all_hosts) * _DISK_SLACK
    free_gb = usage.free / _GB

    summary = (
        f"{free_gb:.1f} GB free on {probe_dir}, need ~{required_gb:.1f} GB "
        f"({len(config.all_hosts)} guest(s) x {per_guest_gb:.1f} GB)"
    )
    if free_gb >= required_gb:
        return CheckResult("Host disk", CheckStatus.PASS, summary)
    return CheckResult("Host disk", CheckStatus.FAIL, summary)


def check_template_or_ova(config: ClusterConfig) -> CheckResult:
    """Check that either the template VM or a built golden OVA is available.

    Args:
        config: The cluster configuration.

    Returns:
        The check result. A deploy needs the OVA; building one needs the
        template VM. Having neither is a hard failure.
    """
    ova = config.virtualbox.template_ova
    if ova.is_file():
        return CheckResult(
            "Template/OVA",
            CheckStatus.PASS,
            f"golden OVA present at {ova} ({ova.stat().st_size / _GB:.1f} GB)",
        )

    if config.virtualbox.vboxmanage.is_file():
        vbox = VBoxManage(config.virtualbox.vboxmanage)
        if vbox.exists(config.virtualbox.template_vm):
            return CheckResult(
                "Template/OVA",
                CheckStatus.WARN,
                f"no golden OVA yet, but template VM '{config.virtualbox.template_vm}' is "
                "registered; run 'vmdeploy template' to build the image first",
            )

    return CheckResult(
        "Template/OVA",
        CheckStatus.FAIL,
        f"neither a golden OVA ({ova}) nor the template VM "
        f"'{config.virtualbox.template_vm}' is available",
    )


def run_preflight(config: ClusterConfig) -> list[CheckResult]:
    """Run every preflight check and return the results in order.

    Args:
        config: The cluster configuration.

    Returns:
        One result per check.
    """
    return [
        check_vboxmanage(config),
        check_ssh_key(config),
        check_key_directory_writable(config),
        check_host_ram(config),
        check_host_disk(config),
        check_template_or_ova(config),
    ]


def worst_status(results: list[CheckResult]) -> CheckStatus:
    """Return the most severe status among a set of results.

    Args:
        results: The check results.

    Returns:
        FAIL if any check failed, else WARN if any warned, else PASS.
    """
    statuses = {result.status for result in results}
    if CheckStatus.FAIL in statuses:
        return CheckStatus.FAIL
    if CheckStatus.WARN in statuses:
        return CheckStatus.WARN
    return CheckStatus.PASS


def format_report(results: list[CheckResult]) -> str:
    """Render preflight results as an aligned text report.

    Args:
        results: The check results.

    Returns:
        A multi-line report suitable for printing.
    """
    width = max((len(result.name) for result in results), default=0)
    lines = [
        f"  [{result.status.value:<4}] {result.name.ljust(width)}  {result.detail}"
        for result in results
    ]
    return "\n".join(lines)
