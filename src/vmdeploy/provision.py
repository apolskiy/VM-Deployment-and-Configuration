"""Golden template export and virtual machine provisioning.

Cloned Debian-family guests share two pieces of state that must be made unique
before a second clone is booted:

``/etc/machine-id``
    Netplan's default ``dhcp-identifier`` is a DUID derived from the machine
    ID. Two clones therefore present the same identifier to the DHCP server and
    are handed the same lease, so one of them silently loses connectivity. The
    target template was confirmed to use this default.

SSH host keys
    Identical host keys across guests defeat host verification and make the
    cluster indistinguishable to a client that pins keys.

Both are regenerated during first boot, and guests are provisioned strictly one
at a time so a clone is always made unique before its successor is started.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Final

from vmdeploy.config import ClusterConfig, HostConfig
from vmdeploy.exceptions import ProvisioningTimeoutError, VmDeployError
from vmdeploy.inventory_service import prepare_template
from vmdeploy.setup import sanitize_image
from vmdeploy.ssh_client import RemoteHost, wait_for_ssh
from vmdeploy.virtualbox import VBoxManage, VMState

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

_REBOOT_SETTLE_SECONDS: Final[int] = 15


def export_golden_template(vbox: VBoxManage, config: ClusterConfig) -> None:
    """Power off the template machine and export it as a golden OVA.

    The template must be shut down first: exporting a running machine is
    refused by VirtualBox, and a snapshot taken mid-write would carry a dirty
    filesystem into every clone.

    Args:
        vbox: The hypervisor wrapper.
        config: The cluster configuration.

    Raises:
        VmDeployError: If the template machine is not registered.
        VirtualBoxError: If the export fails.
    """
    template = config.virtualbox.template_vm
    if not vbox.exists(template):
        raise VmDeployError(
            f"Template machine '{template}' is not registered with VirtualBox; "
            f"registered machines: {list(vbox.list_vms())}"
        )

    _LOG.info("Preparing golden template from %s", template)
    vbox.power_off(template)
    vbox.export_appliance(template, config.virtualbox.template_ova)


_PREBAKE_SNAPSHOT: Final[str] = "vmdeploy-prebake"


def build_golden_template(vbox: VBoxManage, config: ClusterConfig, source_dir: Path) -> None:
    """Bake dependencies into the template, sanitise it, and export a clean OVA.

    The template box is snapshotted first, so every mutation the build makes —
    installing dependencies, and then stripping credentials and build tooling
    from the image — is rolled back afterward. The exported OVA is a clean,
    distributable runtime artefact (no cached registry or git credentials, no
    git/docker/gh), while the template box is returned to exactly its prior
    state, credentials and all, for continued use.

    Args:
        vbox: The hypervisor wrapper.
        config: The cluster configuration.
        source_dir: Local directory holding the Go service sources.

    Raises:
        VmDeployError: If the template is not registered or the build fails.
        ProvisioningTimeoutError: If the template never becomes reachable.
    """
    template = config.virtualbox.template_vm
    if not vbox.exists(template):
        raise VmDeployError(
            f"Template machine '{template}' is not registered with VirtualBox; "
            f"registered machines: {list(vbox.list_vms())}"
        )

    _LOG.info("Building golden template %s (snapshot, bake, sanitise, export, roll back)", template)
    vbox.power_off(template)
    vbox.take_snapshot(template, _PREBAKE_SNAPSHOT)

    try:
        vbox.start_headless(template)
        address = vbox.wait_for_guest_ip(
            template, template, config.virtualbox.boot_timeout_seconds
        )
        wait_for_ssh(address, config.ssh, config.virtualbox.boot_timeout_seconds)

        with RemoteHost(address, config.ssh) as host:
            prepare_template(host, config, source_dir)
            # Strip credentials and build tooling last, so nothing the bake
            # pulled in (e.g. apt caches, tooling) survives into the image.
            sanitize_image(host, config.ssh.user)

        vbox.power_off(template)
        vbox.export_appliance(template, config.virtualbox.template_ova)
        _LOG.info("Golden template exported to %s", config.virtualbox.template_ova)
    finally:
        # Always return the working box to its pre-build state, even on failure.
        vbox.power_off(template)
        vbox.restore_snapshot(template, _PREBAKE_SNAPSHOT)
        vbox.delete_snapshot(template, _PREBAKE_SNAPSHOT)
        _LOG.info("Template box %s rolled back to its pre-build state", template)


def provision_host(vbox: VBoxManage, config: ClusterConfig, host: HostConfig) -> str:
    """Import, boot, and individualise a single cluster guest.

    Args:
        vbox: The hypervisor wrapper.
        config: The cluster configuration.
        host: The host definition to provision.

    Returns:
        The guest's reachable IPv4 address after individualisation.

    Raises:
        VmDeployError: If the guest cannot be provisioned.
        ProvisioningTimeoutError: If the guest never becomes reachable.
    """
    _LOG.info("Provisioning %s (%s)", host.vm_name, host.role.value)

    bridge = vbox.bridge_interface_of(config.virtualbox.template_vm)
    vbox.destroy(host.vm_name)
    vbox.import_appliance(config.virtualbox.template_ova, host.vm_name)
    vbox.configure(
        host.vm_name,
        memory_mb=config.virtualbox.memory_mb,
        cpus=config.virtualbox.cpus,
    )
    if bridge:
        vbox.set_bridge_adapter(host.vm_name, bridge)
    else:
        _LOG.warning(
            "Template %s has no bridged adapter; leaving %s on its imported network "
            "configuration, which may not be reachable from the test host",
            config.virtualbox.template_vm,
            host.vm_name,
        )

    vbox.start_headless(host.vm_name)

    # The clone still carries the template's hostname, so DNS cannot identify
    # it yet. Guest Additions report the lease directly, which is why the
    # additions are a prerequisite documented in the README.
    address = vbox.wait_for_guest_ip(
        host.vm_name, config.virtualbox.template_vm, config.virtualbox.boot_timeout_seconds
    )
    wait_for_ssh(address, config.ssh, config.virtualbox.boot_timeout_seconds)

    _individualise(address, config, host)

    final_address = vbox.wait_for_guest_ip(
        host.vm_name, host.hostname, config.virtualbox.boot_timeout_seconds
    )
    wait_for_ssh(final_address, config.ssh, config.virtualbox.boot_timeout_seconds)

    with RemoteHost(final_address, config.ssh) as guest:
        actual = guest.hostname()
        if actual != host.hostname:
            raise VmDeployError(
                f"{host.vm_name} reports hostname '{actual}' after provisioning, "
                f"expected '{host.hostname}'"
            )

    _LOG.info("%s provisioned at %s", host.hostname, final_address)
    return final_address


def _individualise(address: str, config: ClusterConfig, host: HostConfig) -> None:
    """Give a freshly cloned guest a unique identity, then reboot it.

    Args:
        address: The guest's current IPv4 address.
        config: The cluster configuration.
        host: The host definition being provisioned.

    Raises:
        VmDeployError: If identity commands fail.
    """
    _LOG.info("Individualising %s as %s", address, host.hostname)
    with RemoteHost(address, config.ssh) as guest:
        # A fresh machine ID must exist before the next DHCP request, otherwise
        # this clone and its siblings contend for one lease.
        guest.sudo("rm -f /etc/machine-id /var/lib/dbus/machine-id")
        guest.sudo("systemd-machine-id-setup")
        guest.sudo("sh -c 'cp /etc/machine-id /var/lib/dbus/machine-id'", check=False)

        guest.sudo("rm -f /etc/ssh/ssh_host_*")
        guest.sudo("dpkg-reconfigure -f noninteractive openssh-server", check=False)
        guest.sudo("ssh-keygen -A", check=False)

        guest.sudo(f"hostnamectl set-hostname {host.hostname}")
        _rewrite_hosts_file(guest, host.hostname)

        _LOG.info("Rebooting %s to apply its new identity", host.hostname)
        guest.sudo("systemd-run --on-active=2 --timer-property=AccuracySec=100ms systemctl reboot",
                   check=False)

    # The guest tears down its SSH listener during shutdown; probing before it
    # does would reconnect to the still-live pre-reboot session and report
    # success against the old identity.
    time.sleep(_REBOOT_SETTLE_SECONDS)


def _rewrite_hosts_file(guest: RemoteHost, hostname: str) -> None:
    """Point the 127.0.1.1 entry in /etc/hosts at the new hostname.

    Debian-family systems map the hostname to 127.0.1.1. Leaving the template's
    name there makes ``sudo`` emit resolution warnings and breaks any service
    that resolves its own hostname at start-up.

    Args:
        guest: A connected SSH session to the guest.
        hostname: The guest's new hostname.

    Raises:
        VmDeployError: If the hosts file cannot be rewritten.
    """
    guest.sudo(
        "sh -c "
        f"\"grep -q '^127.0.1.1' /etc/hosts "
        f"&& sed -i 's/^127.0.1.1.*/127.0.1.1\\t{hostname}/' /etc/hosts "
        f"|| printf '127.0.1.1\\t{hostname}\\n' >> /etc/hosts\"",
        check=False,
    )


def provision_cluster(vbox: VBoxManage, config: ClusterConfig) -> dict[str, str]:
    """Provision every cluster guest, one at a time.

    Serial provisioning is deliberate. Booting clones concurrently lets them
    request DHCP leases while still sharing a machine ID, which produces
    duplicate address assignments that are difficult to diagnose after the
    fact.

    Args:
        vbox: The hypervisor wrapper.
        config: The cluster configuration.

    Returns:
        Mapping of hostname to resolved IPv4 address for every guest.

    Raises:
        VmDeployError: If any guest fails to provision.
    """
    addresses: dict[str, str] = {}
    for host in config.all_hosts:
        addresses[host.hostname] = provision_host(vbox, config, host)
    _LOG.info("Cluster provisioned: %s", addresses)
    return addresses


def teardown_cluster(vbox: VBoxManage, config: ClusterConfig) -> None:
    """Destroy every cluster guest, leaving the template untouched.

    Args:
        vbox: The hypervisor wrapper.
        config: The cluster configuration.
    """
    for host in config.all_hosts:
        _LOG.info("Destroying %s", host.vm_name)
        vbox.destroy(host.vm_name)


def cluster_addresses(vbox: VBoxManage, config: ClusterConfig) -> dict[str, str]:
    """Resolve the current address of every running cluster guest.

    Args:
        vbox: The hypervisor wrapper.
        config: The cluster configuration.

    Returns:
        Mapping of hostname to IPv4 address for guests that are running and
        reporting an address. Guests that are stopped or not yet reporting are
        omitted rather than raising, so callers can act on a partial cluster.
    """
    addresses: dict[str, str] = {}
    for host in config.all_hosts:
        if vbox.state(host.vm_name) is not VMState.RUNNING:
            _LOG.warning("%s is not running", host.vm_name)
            continue
        try:
            addresses[host.hostname] = vbox.wait_for_guest_ip(
                host.vm_name, host.hostname, timeout_seconds=30
            )
        except ProvisioningTimeoutError:
            _LOG.warning("%s is running but reported no IPv4 address", host.vm_name)
    return addresses
