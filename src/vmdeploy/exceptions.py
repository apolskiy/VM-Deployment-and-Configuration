"""Exception hierarchy for the vmdeploy automation package.

Every failure raised by this package derives from :class:`VmDeployError`, so
callers can catch the whole family with a single ``except`` clause while still
being able to discriminate specific failure modes when they need to retry or
report differently.
"""

from __future__ import annotations


class VmDeployError(Exception):
    """Base class for every error raised by the vmdeploy package."""


class ConfigurationError(VmDeployError):
    """Raised when cluster configuration is missing, malformed, or invalid."""


class SSHConnectionError(VmDeployError):
    """Raised when an SSH transport cannot be established or is lost."""


class RemoteCommandError(VmDeployError):
    """Raised when a remote command exits with a non-zero status.

    Attributes:
        command: The command string that was executed remotely.
        exit_status: The exit status returned by the remote shell.
        stdout: Captured standard output, decoded as UTF-8.
        stderr: Captured standard error, decoded as UTF-8.
    """

    def __init__(self, command: str, exit_status: int, stdout: str, stderr: str) -> None:
        """Initialise the error with full remote execution context.

        Args:
            command: The command string that was executed remotely.
            exit_status: The exit status returned by the remote shell.
            stdout: Captured standard output, decoded as UTF-8.
            stderr: Captured standard error, decoded as UTF-8.
        """
        self.command = command
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"Remote command exited with status {exit_status}: {command}\n"
            f"stderr: {stderr.strip() or '<empty>'}"
        )


class VirtualBoxError(VmDeployError):
    """Raised when a ``VBoxManage`` invocation fails.

    Attributes:
        args_used: The argument vector passed to ``VBoxManage``.
        exit_status: The process exit status.
        stderr: Captured standard error from the process.
    """

    def __init__(self, args_used: tuple[str, ...], exit_status: int, stderr: str) -> None:
        """Initialise the error with the failed VBoxManage invocation.

        Args:
            args_used: The argument vector passed to ``VBoxManage``.
            exit_status: The process exit status.
            stderr: Captured standard error from the process.
        """
        self.args_used = args_used
        self.exit_status = exit_status
        self.stderr = stderr
        super().__init__(
            f"VBoxManage {' '.join(args_used)} failed with status {exit_status}: "
            f"{stderr.strip() or '<empty>'}"
        )


class ProvisioningTimeoutError(VmDeployError):
    """Raised when a provisioning step does not converge within its deadline."""


class InventoryError(VmDeployError):
    """Raised when the encrypted inventory cannot be read, parsed, or written."""


class DecryptionError(InventoryError):
    """Raised when inventory ciphertext fails authentication or decryption.

    An AES-256-GCM authentication failure means the payload was truncated,
    corrupted, or tampered with, or that the wrong key was supplied. All four
    cases are security relevant, so they are surfaced distinctly rather than
    being folded into a generic parse error.
    """


class WebsiteFetchError(VmDeployError):
    """Raised when the published static site cannot be retrieved or unpacked."""
