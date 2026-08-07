"""Automation package for VirtualBox cluster provisioning and validation.

This package provisions a three-node VirtualBox cluster from a golden OVA
template, configures Apache as a load balancer on the jump station and as
static web servers on the backends, and maintains an AES-256-GCM encrypted
inventory of deployed hosts that is served by a companion Go service.

Modules:
    config: Typed, validated cluster configuration loaded from TOML.
    exceptions: Exception hierarchy shared by every automation module.
    ssh_client: Context-managed Paramiko SSH and SFTP wrapper.
    virtualbox: Typed wrapper around the ``VBoxManage`` command line tool.
    provision: Template export plus VM import, boot, and IP discovery.
    apache: Jump station load balancer and backend web server configuration.
    website: Retrieval of the latest static build of the published site.
    inventory: Client for the AES-256-GCM encrypted host registry.
    cli: Command line orchestration entry point.
"""

from vmdeploy.exceptions import (
    ConfigurationError,
    DecryptionError,
    InventoryError,
    ProvisioningTimeoutError,
    RemoteCommandError,
    SSHConnectionError,
    VirtualBoxError,
    VmDeployError,
    WebsiteFetchError,
)

__version__ = "1.0.0"

__all__ = [
    "ConfigurationError",
    "DecryptionError",
    "InventoryError",
    "ProvisioningTimeoutError",
    "RemoteCommandError",
    "SSHConnectionError",
    "VirtualBoxError",
    "VmDeployError",
    "WebsiteFetchError",
    "__version__",
]
