"""Context-managed Paramiko SSH and SFTP wrapper.

The reference implementation this package supersedes issued remote commands
with ``exec_command`` and never inspected the exit status, so a failed
``apt install`` looked identical to a successful one. Every helper here reads
the channel exit status and raises :class:`RemoteCommandError` by default.

Privilege escalation reads its password from the ``VMDEPLOY_SUDO_PASSWORD``
environment variable. It is never written to a file in the repository, never
echoed to logs, and is fed to ``sudo -S`` over stdin rather than being
interpolated into a command line where it would be visible in the guest's
process table.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import paramiko

from vmdeploy.config import SSHConfig
from vmdeploy.exceptions import (
    ProvisioningTimeoutError,
    RemoteCommandError,
    SSHConnectionError,
)

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

SUDO_PASSWORD_ENV: Final[str] = "VMDEPLOY_SUDO_PASSWORD"

_RETRY_INTERVAL_SECONDS: Final[float] = 5.0


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of a single remote command.

    Attributes:
        command: The command string that was executed.
        exit_status: The exit status reported by the remote shell.
        stdout: Captured standard output, decoded as UTF-8.
        stderr: Captured standard error, decoded as UTF-8.
    """

    command: str
    exit_status: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """Whether the command completed successfully.

        Returns:
            True if the exit status was zero.
        """
        return self.exit_status == 0

    def lines(self) -> list[str]:
        """Split standard output into stripped, non-empty lines.

        Returns:
            Output lines with surrounding whitespace removed and blank lines
            discarded.
        """
        return [line.strip() for line in self.stdout.splitlines() if line.strip()]


def sudo_password() -> str:
    """Read the sudo password from the environment.

    Returns:
        The configured sudo password.

    Raises:
        SSHConnectionError: If the environment variable is unset or empty.
            This is surfaced as a connection-class error because it blocks all
            privileged remote work in exactly the same way a failed login does.
    """
    password = os.environ.get(SUDO_PASSWORD_ENV, "")
    if not password:
        raise SSHConnectionError(
            f"Privileged remote commands require the {SUDO_PASSWORD_ENV} environment "
            "variable to be set, or passwordless sudo to be configured on the guest"
        )
    return password


class RemoteHost:
    """An SSH connection to one cluster host.

    The instance is a context manager; the underlying transport is closed on
    exit even when the body raises.

    Attributes:
        address: The hostname or IP address this instance connects to.
    """

    def __init__(self, address: str, ssh_config: SSHConfig) -> None:
        """Prepare a connection descriptor without connecting.

        Args:
            address: Hostname or IP address of the target guest.
            ssh_config: Shared credentials and timeouts.
        """
        self.address = address
        self._ssh_config = ssh_config
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None

    def __enter__(self) -> Self:
        """Open the SSH transport.

        Returns:
            This instance, connected and ready for command execution.
        """
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the SSH transport, releasing SFTP resources first.

        Args:
            exc_type: Type of any exception raised in the managed block.
            exc_value: The exception raised in the managed block, if any.
            traceback: Traceback for the exception, if any.
        """
        self.close()

    @property
    def client(self) -> paramiko.SSHClient:
        """The live Paramiko client.

        Returns:
            The connected Paramiko client.

        Raises:
            SSHConnectionError: If accessed before :meth:`connect`.
        """
        if self._client is None:
            raise SSHConnectionError(f"Not connected to {self.address}; call connect() first")
        return self._client

    def connect(self) -> None:
        """Establish the SSH transport.

        Raises:
            SSHConnectionError: If authentication fails, the key is unusable,
                or the host is unreachable within the configured timeout.
        """
        if self._client is not None:
            return

        key_path = self._ssh_config.key_path
        if not key_path.is_file():
            raise SSHConnectionError(f"SSH private key not found at {key_path}")

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        _LOG.info("Connecting to %s as %s", self.address, self._ssh_config.user)
        try:
            client.connect(
                hostname=self.address,
                username=self._ssh_config.user,
                key_filename=str(key_path),
                timeout=self._ssh_config.connect_timeout_seconds,
                auth_timeout=self._ssh_config.connect_timeout_seconds,
                banner_timeout=self._ssh_config.connect_timeout_seconds,
                look_for_keys=False,
                # The configured key may be passphrase-encrypted, in which case
                # loading the file alone raises. Allowing the agent lets
                # paramiko fall through to an agent-loaded copy of the key
                # (including the Windows OpenSSH named-pipe agent), so an
                # operator whose key is in their agent needs no passphrase.
                allow_agent=True,
            )
        except paramiko.AuthenticationException as exc:
            client.close()
            raise SSHConnectionError(
                f"Authentication failed for {self._ssh_config.user}@{self.address} "
                f"using key {key_path}: {exc}"
            ) from exc
        except (paramiko.SSHException, OSError, socket.error) as exc:
            client.close()
            raise SSHConnectionError(f"Cannot reach {self.address} over SSH: {exc}") from exc

        self._client = client

    def close(self) -> None:
        """Close SFTP and SSH resources, tolerating an already-dead transport."""
        if self._sftp is not None:
            try:
                self._sftp.close()
            except (OSError, paramiko.SSHException):
                _LOG.debug("SFTP channel to %s already closed", self.address)
            self._sftp = None

        if self._client is not None:
            self._client.close()
            self._client = None
            _LOG.debug("Closed SSH connection to %s", self.address)

    def run(self, command: str, *, check: bool = True, timeout: int | None = None) -> CommandResult:
        """Execute a command and capture its full result.

        Args:
            command: The command to execute in the remote shell.
            check: Whether a non-zero exit status raises.
            timeout: Per-command timeout in seconds. Defaults to the value in
                the SSH configuration.

        Returns:
            The captured command result.

        Raises:
            RemoteCommandError: If the command exits non-zero and ``check``
                is True.
            SSHConnectionError: If the transport fails mid-command.
        """
        effective_timeout = timeout or self._ssh_config.command_timeout_seconds
        _LOG.debug("[%s] $ %s", self.address, command)
        try:
            _, stdout, stderr = self.client.exec_command(command, timeout=effective_timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            status = stdout.channel.recv_exit_status()
        except (paramiko.SSHException, OSError, socket.error) as exc:
            raise SSHConnectionError(f"Command failed on {self.address}: {command}: {exc}") from exc

        result = CommandResult(command=command, exit_status=status, stdout=out, stderr=err)
        if check and not result.ok:
            raise RemoteCommandError(command, status, out, err)
        return result

    def sudo(
        self, command: str, *, check: bool = True, timeout: int | None = None
    ) -> CommandResult:
        """Execute a command with root privileges.

        Passwordless sudo is attempted first with ``sudo -n``. Only if that is
        refused is the password read from the environment and piped to
        ``sudo -S``, so a correctly configured host never needs the variable.

        Args:
            command: The command to execute as root.
            check: Whether a non-zero exit status raises.
            timeout: Per-command timeout in seconds.

        Returns:
            The captured command result.

        Raises:
            RemoteCommandError: If the privileged command fails and ``check``
                is True.
            SSHConnectionError: If sudo requires a password that is not
                available in the environment.
        """
        passwordless = self.run(f"sudo -n {command}", check=False, timeout=timeout)
        if passwordless.ok:
            return passwordless

        password = sudo_password()
        # -S reads the password from stdin; -p '' suppresses the prompt so it
        # cannot be mistaken for command output by callers parsing stdout.
        escalated = f"sudo -S -p '' {command}"
        _LOG.debug("[%s] $ sudo (password) %s", self.address, command)
        try:
            stdin, stdout, stderr = self.client.exec_command(
                escalated, timeout=timeout or self._ssh_config.command_timeout_seconds
            )
            stdin.write(f"{password}\n")
            stdin.flush()
            stdin.channel.shutdown_write()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            status = stdout.channel.recv_exit_status()
        except (paramiko.SSHException, OSError, socket.error) as exc:
            raise SSHConnectionError(
                f"Privileged command failed on {self.address}: {command}: {exc}"
            ) from exc

        result = CommandResult(command=escalated, exit_status=status, stdout=out, stderr=err)
        if check and not result.ok:
            raise RemoteCommandError(escalated, status, out, err)
        return result

    def sftp(self) -> paramiko.SFTPClient:
        """Open, or reuse, the SFTP channel for this connection.

        Returns:
            A live SFTP client bound to this SSH transport.

        Raises:
            SSHConnectionError: If the SFTP subsystem cannot be started.
        """
        if self._sftp is None:
            try:
                self._sftp = self.client.open_sftp()
            except (paramiko.SSHException, OSError) as exc:
                raise SSHConnectionError(f"Cannot open SFTP to {self.address}: {exc}") from exc
        return self._sftp

    def put_bytes(self, payload: bytes, remote_path: str, *, mode: int = 0o644) -> None:
        """Write bytes to a remote file, replacing any existing content.

        The write targets a temporary path in the user's home directory and is
        then moved into place with ``install``, so the destination may live in
        a root-owned directory without the SFTP session needing root.

        Args:
            payload: Raw bytes to write.
            remote_path: Absolute destination path in the guest.
            mode: POSIX permission bits applied to the destination.

        Raises:
            SSHConnectionError: If the SFTP transfer fails.
            RemoteCommandError: If the privileged move into place fails.
        """
        staging = f"/tmp/vmdeploy-{os.urandom(8).hex()}"
        sftp = self.sftp()
        try:
            with sftp.file(staging, "wb") as handle:
                handle.write(payload)
        except (OSError, paramiko.SSHException) as exc:
            raise SSHConnectionError(
                f"Failed staging {len(payload)} bytes to {self.address}:{staging}: {exc}"
            ) from exc

        self.sudo(f"install -D -m {mode:o} {staging} {remote_path}")
        self.run(f"rm -f {staging}", check=False)
        _LOG.debug("Wrote %d bytes to %s:%s", len(payload), self.address, remote_path)

    def put_file(self, local_path: Path, remote_path: str, *, mode: int = 0o644) -> None:
        """Upload a local file to the guest.

        Args:
            local_path: The local file to upload.
            remote_path: Absolute destination path in the guest.
            mode: POSIX permission bits applied to the destination.

        Raises:
            SSHConnectionError: If the local file cannot be read or the
                transfer fails.
        """
        try:
            payload = local_path.read_bytes()
        except OSError as exc:
            raise SSHConnectionError(f"Cannot read local file {local_path}: {exc}") from exc
        self.put_bytes(payload, remote_path, mode=mode)

    def get_bytes(self, remote_path: str) -> bytes:
        """Read a remote file in full.

        Args:
            remote_path: Absolute path of the file to read in the guest.

        Returns:
            The file's raw bytes.

        Raises:
            SSHConnectionError: If the file cannot be read.
        """
        try:
            with self.sftp().file(remote_path, "rb") as handle:
                return bytes(handle.read())
        except (OSError, paramiko.SSHException) as exc:
            raise SSHConnectionError(
                f"Cannot read {self.address}:{remote_path}: {exc}"
            ) from exc

    def hostname(self) -> str:
        """Return the guest's configured hostname.

        Returns:
            The hostname reported by the guest.
        """
        return self.run("hostname").stdout.strip()

    def primary_ipv4(self) -> str:
        """Return the guest's primary non-loopback IPv4 address.

        ``hostname -I`` is preferred over ``hostname -i`` because the latter
        resolves through ``/etc/hosts`` and frequently returns ``127.0.1.1``
        on a freshly cloned Debian-family guest.

        Returns:
            The first routable IPv4 address, or an empty string if the guest
            reports none.
        """
        result = self.run(
            "hostname -I | tr ' ' '\\n' | grep -E '^[0-9]+(\\.[0-9]+){3}$' | head -n 1",
            check=False,
        )
        return result.stdout.strip()


def wait_for_ssh(address: str, ssh_config: SSHConfig, timeout_seconds: int) -> None:
    """Block until a guest accepts SSH connections.

    Args:
        address: Hostname or IP address of the guest.
        ssh_config: Shared credentials and timeouts.
        timeout_seconds: Total time to keep retrying before giving up.

    Raises:
        ProvisioningTimeoutError: If the guest does not accept an
            authenticated SSH session within the deadline.
    """
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    last_error = "no attempt completed"

    while time.monotonic() < deadline:
        attempt += 1
        try:
            with RemoteHost(address, ssh_config) as host:
                host.run("true")
            _LOG.info("%s accepted SSH after %d attempt(s)", address, attempt)
            return
        except SSHConnectionError as exc:
            last_error = str(exc)
            _LOG.debug("Attempt %d: %s not ready (%s)", attempt, address, last_error)
            time.sleep(_RETRY_INTERVAL_SECONDS)

    raise ProvisioningTimeoutError(
        f"{address} did not accept SSH within {timeout_seconds}s "
        f"after {attempt} attempt(s); last error: {last_error}"
    )
