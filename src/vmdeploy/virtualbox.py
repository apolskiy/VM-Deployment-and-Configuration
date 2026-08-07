"""Typed wrapper around the ``VBoxManage`` command line tool.

All hypervisor interaction goes through :class:`VBoxManage`. Commands are
invoked as argument vectors rather than shell strings so that Windows paths
containing spaces, such as ``C:\\Program Files\\Oracle\\VirtualBox``, need no
quoting and cannot be reinterpreted by a shell.
"""

from __future__ import annotations

import logging
import re
import socket
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

from vmdeploy.exceptions import ProvisioningTimeoutError, VirtualBoxError

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

_IPV4_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_POLL_INTERVAL_SECONDS: Final[float] = 5.0

# Guest interfaces that never carry the reachable LAN address. The template is
# cloned from a host with Docker installed, so every guest exposes a docker0
# bridge (172.17.0.1); selecting it instead of the real NIC is the exact
# failure this filtering prevents.
_VIRTUAL_IFACE_PREFIXES: Final[tuple[str, ...]] = (
    "docker",
    "br-",
    "veth",
    "virbr",
    "vboxnet",
    "lo",
    "tun",
    "tap",
    "cni",
    "flannel",
)

# Matches the Guest Additions network properties emitted by
# ``guestproperty enumerate``: name and IPv4 address per interface index.
_NET_PROPERTY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"/VirtualBox/GuestInfo/Net/(\d+)/(Name|V4/IP)\s+=\s+'([^']*)'"
)

# Export of a multi-gigabyte appliance and import of the same are the two
# slowest operations in the pipeline; they get their own generous ceiling.
_APPLIANCE_TIMEOUT_SECONDS: Final[int] = 3600


class VMState(Enum):
    """Lifecycle state of a registered virtual machine.

    Attributes:
        RUNNING: The guest is executing.
        POWERED_OFF: The guest is registered but not executing.
        SAVED: The guest's execution state is persisted to disk.
        ABORTED: The guest terminated abnormally.
        UNKNOWN: The state string was not recognised.
        ABSENT: No machine with this name is registered.
    """

    RUNNING = "running"
    POWERED_OFF = "poweroff"
    SAVED = "saved"
    ABORTED = "aborted"
    UNKNOWN = "unknown"
    ABSENT = "absent"

    @classmethod
    def parse(cls, raw: str) -> VMState:
        """Map a raw ``VBoxManage`` state string onto this enum.

        Args:
            raw: The state string reported by ``showvminfo``.

        Returns:
            The matching state, or :attr:`UNKNOWN` if unrecognised.
        """
        normalised = raw.strip().strip('"').lower()
        for state in cls:
            if state.value == normalised:
                return state
        return cls.UNKNOWN


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """The outcome of a ``VBoxManage`` invocation.

    Attributes:
        args_used: The argument vector that was executed.
        exit_status: The process exit status.
        stdout: Captured standard output.
        stderr: Captured standard error.
    """

    args_used: tuple[str, ...]
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether the invocation succeeded.

        Returns:
            True if the exit status was zero.
        """
        return self.exit_status == 0


class VBoxManage:
    """Executes ``VBoxManage`` subcommands against the local hypervisor.

    Attributes:
        executable: Path to the ``VBoxManage`` binary.
    """

    def __init__(self, executable: Path) -> None:
        """Bind the wrapper to a ``VBoxManage`` executable.

        Args:
            executable: Path to the ``VBoxManage`` binary.

        Raises:
            VirtualBoxError: If the executable does not exist.
        """
        if not executable.is_file():
            raise VirtualBoxError((str(executable),), -1, "VBoxManage executable not found")
        self.executable = executable

    def run(
        self, *args: str, check: bool = True, timeout: int = 120
    ) -> ProcessResult:
        """Invoke ``VBoxManage`` with the given arguments.

        Args:
            *args: Subcommand and its arguments.
            check: Whether a non-zero exit status raises.
            timeout: Seconds to wait before terminating the process.

        Returns:
            The captured process result.

        Raises:
            VirtualBoxError: If the invocation fails and ``check`` is True, or
                if the process exceeds its timeout.
        """
        argv = [str(self.executable), *args]
        _LOG.debug("VBoxManage %s", " ".join(args))
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise VirtualBoxError(args, -1, f"timed out after {timeout}s") from exc
        except OSError as exc:
            raise VirtualBoxError(args, -1, str(exc)) from exc

        result = ProcessResult(
            args_used=args,
            exit_status=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
        if check and not result.ok:
            raise VirtualBoxError(args, result.exit_status, result.stderr)
        return result

    def version(self) -> str:
        """Return the installed VirtualBox version.

        Returns:
            The version string reported by ``VBoxManage --version``.
        """
        return self.run("--version").stdout.strip()

    def list_vms(self) -> tuple[str, ...]:
        """List every registered virtual machine name.

        Returns:
            Registered machine names in the order reported by VirtualBox.
        """
        result = self.run("list", "vms")
        return tuple(
            match.group(1)
            for match in (re.match(r'^"(.+)"\s+\{', line) for line in result.stdout.splitlines())
            if match
        )

    def exists(self, vm_name: str) -> bool:
        """Check whether a machine is registered.

        Args:
            vm_name: The machine name to look for.

        Returns:
            True if a machine with this name is registered.
        """
        return vm_name in self.list_vms()

    def state(self, vm_name: str) -> VMState:
        """Return the lifecycle state of a machine.

        Args:
            vm_name: The machine to query.

        Returns:
            The machine's state, or :attr:`VMState.ABSENT` if unregistered.
        """
        result = self.run("showvminfo", vm_name, "--machinereadable", check=False)
        if not result.ok:
            return VMState.ABSENT
        for line in result.stdout.splitlines():
            if line.startswith("VMState="):
                return VMState.parse(line.split("=", 1)[1])
        return VMState.UNKNOWN

    def power_off(self, vm_name: str, *, wait_seconds: int = 120) -> None:
        """Power off a running machine and wait for it to settle.

        A machine that is already stopped is left alone, so this is safe to
        call unconditionally before an export or delete.

        Args:
            vm_name: The machine to stop.
            wait_seconds: How long to wait for the state to become stopped.

        Raises:
            ProvisioningTimeoutError: If the machine is still running after
                the wait period.
        """
        if self.state(vm_name) is not VMState.RUNNING:
            _LOG.debug("%s is not running; no power off needed", vm_name)
            return

        _LOG.info("Powering off %s", vm_name)
        self.run("controlvm", vm_name, "acpipowerbutton", check=False)

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if self.state(vm_name) is not VMState.RUNNING:
                _LOG.info("%s powered off cleanly", vm_name)
                return
            time.sleep(_POLL_INTERVAL_SECONDS)

        _LOG.warning("%s ignored ACPI shutdown; forcing power off", vm_name)
        self.run("controlvm", vm_name, "poweroff", check=False)
        time.sleep(_POLL_INTERVAL_SECONDS)
        if self.state(vm_name) is VMState.RUNNING:
            raise ProvisioningTimeoutError(f"{vm_name} could not be powered off")

    def export_appliance(self, vm_name: str, destination: Path) -> None:
        """Export a powered-off machine to an OVA appliance.

        Args:
            vm_name: The machine to export.
            destination: Path the OVA is written to. Its parent is created if
                needed and any existing file at the path is replaced.

        Raises:
            VirtualBoxError: If the machine is running or the export fails.
        """
        if self.state(vm_name) is VMState.RUNNING:
            raise VirtualBoxError(
                ("export", vm_name), -1, "machine must be powered off before export"
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            _LOG.info("Replacing existing appliance at %s", destination)
            destination.unlink()

        _LOG.info("Exporting %s to %s (this takes several minutes)", vm_name, destination)
        self.run(
            "export",
            vm_name,
            "--output",
            str(destination),
            "--ovf20",
            timeout=_APPLIANCE_TIMEOUT_SECONDS,
        )
        _LOG.info("Exported %s (%.2f GB)", destination.name, destination.stat().st_size / 1024**3)

    def import_appliance(self, source: Path, vm_name: str) -> None:
        """Import an OVA appliance under a specific machine name.

        Args:
            source: The OVA file to import.
            vm_name: The name to register the imported machine under.

        Raises:
            VirtualBoxError: If the appliance is missing or the import fails.
        """
        if not source.is_file():
            raise VirtualBoxError(("import", str(source)), -1, "appliance file not found")

        _LOG.info("Importing %s as %s", source.name, vm_name)
        self.run(
            "import",
            str(source),
            "--vsys",
            "0",
            "--vmname",
            vm_name,
            timeout=_APPLIANCE_TIMEOUT_SECONDS,
        )

    def configure(self, vm_name: str, *, memory_mb: int, cpus: int) -> None:
        """Apply guest sizing and a bridged network adapter.

        The adapter is bridged onto the same physical interface the template
        used, so provisioned guests obtain addresses from the same DHCP scope
        and are reachable from the host running the test suite.

        Args:
            vm_name: The machine to reconfigure.
            memory_mb: RAM in megabytes.
            cpus: Virtual CPU count.

        Raises:
            VirtualBoxError: If the machine cannot be reconfigured.
        """
        _LOG.info("Configuring %s with %d MB RAM and %d vCPU", vm_name, memory_mb, cpus)
        self.run("modifyvm", vm_name, "--memory", str(memory_mb), "--cpus", str(cpus))

    def set_bridge_adapter(self, vm_name: str, bridge_interface: str) -> None:
        """Bridge the machine's first adapter onto a host interface.

        Args:
            vm_name: The machine to reconfigure.
            bridge_interface: Host interface name to bridge onto.

        Raises:
            VirtualBoxError: If the adapter cannot be reconfigured.
        """
        self.run(
            "modifyvm",
            vm_name,
            "--nic1",
            "bridged",
            "--bridgeadapter1",
            bridge_interface,
        )

    def bridge_interface_of(self, vm_name: str) -> str:
        """Read the host interface a machine's first adapter is bridged to.

        Args:
            vm_name: The machine to inspect.

        Returns:
            The bridged host interface name, or an empty string if the
            adapter is not bridged.
        """
        result = self.run("showvminfo", vm_name, "--machinereadable", check=False)
        for line in result.stdout.splitlines():
            if line.startswith("bridgeadapter1="):
                return line.split("=", 1)[1].strip().strip('"')
        return ""

    def start_headless(self, vm_name: str) -> None:
        """Start a machine with no attached display.

        Args:
            vm_name: The machine to start.

        Raises:
            VirtualBoxError: If the machine fails to start.
        """
        if self.state(vm_name) is VMState.RUNNING:
            _LOG.debug("%s is already running", vm_name)
            return
        _LOG.info("Starting %s headless", vm_name)
        self.run("startvm", vm_name, "--type", "headless", timeout=300)

    def destroy(self, vm_name: str, *, attempts: int = 3) -> None:
        """Power off, unregister, and delete a machine and its disks.

        The delete is retried and then verified. VirtualBox briefly holds a
        lock on a machine after it powers off, so an ``unregistervm --delete``
        issued immediately can fail while the session is still closing. A single
        unchecked attempt therefore left stray VMs behind; this retries until
        the machine is genuinely gone and raises if it is not.

        Args:
            vm_name: The machine to remove. Absent machines are ignored, so
                this is safe to call when tearing down a partial deployment.
            attempts: How many times to try the delete before giving up.

        Raises:
            VirtualBoxError: If the machine is still registered after every
                attempt, so a stale VM never passes silently.
        """
        if not self.exists(vm_name):
            _LOG.debug("%s is not registered; nothing to destroy", vm_name)
            return
        self.power_off(vm_name)

        last_error = ""
        for attempt in range(1, attempts + 1):
            _LOG.info("Unregistering and deleting %s (attempt %d/%d)", vm_name, attempt, attempts)
            result = self.run("unregistervm", vm_name, "--delete", check=False, timeout=300)
            if not self.exists(vm_name):
                _LOG.info("%s removed", vm_name)
                return
            last_error = result.stderr.strip() or result.stdout.strip() or "still registered"
            _LOG.warning(
                "%s still present after delete attempt %d (%s); retrying",
                vm_name,
                attempt,
                last_error,
            )
            time.sleep(_POLL_INTERVAL_SECONDS)

        raise VirtualBoxError(
            ("unregistervm", vm_name, "--delete"),
            -1,
            f"{vm_name} could not be deleted after {attempts} attempts; last error: {last_error}",
        )

    def guest_property(self, vm_name: str, key: str) -> str:
        """Read a guest property published by the Guest Additions.

        Args:
            vm_name: The machine to query.
            key: The guest property path.

        Returns:
            The property value, or an empty string if unset or unavailable.
        """
        result = self.run("guestproperty", "get", vm_name, key, check=False)
        text = result.stdout.strip()
        if not result.ok or not text.startswith("Value:"):
            return ""
        return text.split(":", 1)[1].strip()

    def guest_ipv4_interfaces(self, vm_name: str) -> list[tuple[str, str]]:
        """Enumerate every IPv4 interface the Guest Additions report.

        Args:
            vm_name: The machine to query.

        Returns:
            A list of ``(interface_name, ipv4)`` pairs in interface-index
            order. Empty if the additions have not yet published network state.
        """
        result = self.run("guestproperty", "enumerate", vm_name, check=False)
        if not result.ok:
            return []

        names: dict[int, str] = {}
        ips: dict[int, str] = {}
        for match in _NET_PROPERTY_PATTERN.finditer(result.stdout):
            index, field, value = int(match.group(1)), match.group(2), match.group(3)
            if field == "Name":
                names[index] = value
            else:
                ips[index] = value

        interfaces: list[tuple[str, str]] = []
        for index in sorted(ips):
            address = ips[index]
            if _IPV4_PATTERN.match(address):
                interfaces.append((names.get(index, f"net{index}"), address))
        return interfaces

    def wait_for_guest_ip(self, vm_name: str, hostname: str, timeout_seconds: int) -> str:
        """Discover a running guest's reachable IPv4 address.

        Interface selection matters: a guest cloned from a Docker-enabled
        template exposes a ``docker0`` bridge (172.17.0.1) that is unreachable
        from the host, alongside the real bridged NIC. Virtual interfaces are
        filtered out and the address sharing the host's LAN subnet is
        preferred, so the reachable NIC is always chosen over the bridge.

        Two independent mechanisms are tried on every poll because neither is
        universally available: the Guest Additions enumeration is empty on a
        guest without the additions, and DNS resolution depends on the DHCP
        server registering the guest's hostname.

        Args:
            vm_name: The machine to query.
            hostname: The guest's hostname, used for the DNS fallback.
            timeout_seconds: Total time to keep polling.

        Returns:
            The discovered IPv4 address.

        Raises:
            ProvisioningTimeoutError: If no reachable address is found before
                the deadline expires.
        """
        host_lan = _host_lan_ipv4()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            interfaces = self.guest_ipv4_interfaces(vm_name)
            selected = _select_lan_address(interfaces, host_lan)
            if selected:
                _LOG.info(
                    "%s reported IPv4 %s via Guest Additions (candidates: %s)",
                    vm_name,
                    selected,
                    interfaces,
                )
                return selected

            resolved = _resolve_ipv4(hostname)
            if resolved:
                _LOG.info("%s resolved to IPv4 %s via DNS", hostname, resolved)
                return resolved

            time.sleep(_POLL_INTERVAL_SECONDS)

        raise ProvisioningTimeoutError(
            f"Could not determine a reachable IPv4 address for {vm_name} "
            f"(hostname {hostname}) within {timeout_seconds}s; check that the guest "
            "obtained a DHCP lease on its bridged adapter"
        )


def _resolve_ipv4(hostname: str) -> str:
    """Resolve a hostname to its first IPv4 address.

    Args:
        hostname: The name to resolve.

    Returns:
        The resolved dotted-quad address, or an empty string if the name does
        not resolve to an IPv4 address.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, family=socket.AF_INET)
    except socket.gaierror:
        return ""
    for info in infos:
        address = info[4][0]
        if isinstance(address, str) and _IPV4_PATTERN.match(address):
            return address
    return ""


def _is_virtual_interface(name: str) -> bool:
    """Report whether an interface name is a virtual bridge or tunnel.

    Args:
        name: The interface name reported by the Guest Additions.

    Returns:
        True if the interface never carries the reachable LAN address.
    """
    lowered = name.lower()
    return any(lowered.startswith(prefix) for prefix in _VIRTUAL_IFACE_PREFIXES)


def _is_usable_address(address: str) -> bool:
    """Report whether an address could be a reachable LAN address.

    Args:
        address: A dotted-quad IPv4 address.

    Returns:
        True unless the address is loopback or link-local, neither of which is
        reachable from another host.
    """
    return not address.startswith(("127.", "169.254."))


def _same_slash24(left: str, right: str) -> bool:
    """Report whether two IPv4 addresses share a /24 prefix.

    Args:
        left: A dotted-quad IPv4 address.
        right: A dotted-quad IPv4 address.

    Returns:
        True if the first three octets match.
    """
    return left.rsplit(".", 1)[0] == right.rsplit(".", 1)[0]


def _select_lan_address(interfaces: list[tuple[str, str]], host_lan: str) -> str:
    """Choose the reachable LAN address from enumerated guest interfaces.

    Args:
        interfaces: ``(interface_name, ipv4)`` pairs from the guest.
        host_lan: The automation host's own LAN address, used to prefer the
            interface on the same subnet. May be empty if undetectable.

    Returns:
        The selected address, or an empty string if no interface qualifies.
    """
    candidates = [
        address
        for name, address in interfaces
        if not _is_virtual_interface(name) and _is_usable_address(address)
    ]
    if not candidates:
        return ""

    if host_lan:
        for address in candidates:
            if _same_slash24(address, host_lan):
                return address

    # No subnet match: prefer common LAN ranges over the Docker default
    # (172.16/12) before falling back to the first physical interface.
    for address in candidates:
        if address.startswith(("192.168.", "10.")):
            return address
    return candidates[0]


def _host_lan_ipv4() -> str:
    """Determine the automation host's primary LAN IPv4 address.

    Opens a UDP socket toward a public address to learn which local interface
    the OS would route through. No packets are sent; the call only resolves the
    outbound source address.

    Returns:
        The host's LAN IPv4 address, or an empty string if it cannot be
        determined.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
    except OSError:
        return ""
    return address if isinstance(address, str) and _IPV4_PATTERN.match(address) else ""
