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
from pathlib import Path
from typing import Final

from vmdeploy.config import ClusterConfig, HostConfig
from vmdeploy.exceptions import ProvisioningTimeoutError, VmDeployError
from vmdeploy.inventory_service import prepare_template
from vmdeploy.seed import build_seed_iso
from vmdeploy.setup import arm_cloud_init, sanitize_image
from vmdeploy.ssh_client import RemoteHost, wait_for_ssh
from vmdeploy.virtualbox import VBoxManage, VMState

_LOG: Final[logging.Logger] = logging.getLogger(__name__)


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
            # Arm cloud-init after sanitising, so its clean also discards the
            # logs the bake and the sanitise pass just wrote. Clones then take
            # their identity from the seed attached at import.
            arm_cloud_init(host)

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

    # The guest takes its identity from a seed read on first boot, so it is
    # built and inserted before the machine is ever started. Nothing is
    # configured over SSH afterwards, and no reboot is needed to apply it.
    seed_path = _seed_directory(config) / f"{host.vm_name}-seed.iso"
    build_seed_iso(seed_path, host.hostname, config.ssh.user, _deployer_public_key(config))
    vbox.attach_optical(host.vm_name, seed_path)

    vbox.start_headless(host.vm_name)

    address = vbox.wait_for_guest_ip(
        host.vm_name, host.hostname, config.virtualbox.boot_timeout_seconds
    )
    wait_for_ssh(address, config.ssh, config.virtualbox.boot_timeout_seconds)

    with RemoteHost(address, config.ssh) as guest:
        _wait_for_cloud_init(guest, config.virtualbox.boot_timeout_seconds)
        actual = guest.hostname()
        if actual != host.hostname:
            raise VmDeployError(
                f"{host.vm_name} reports hostname '{actual}' after provisioning, "
                f"expected '{host.hostname}'. The cloud-init seed was not applied."
            )

    _LOG.info("%s provisioned at %s", host.hostname, address)
    return address


def _seed_directory(config: ClusterConfig) -> Path:
    """Return the directory holding per-guest seed images.

    The seeds sit beside the golden image they pair with. They stay on disk for
    the life of the guest: cloud-init re-reads the datasource on every boot, and
    an absent one would look like a new instance and re-run configuration. They
    hold nothing secret — a hostname and a public key.

    Args:
        config: The cluster configuration.

    Returns:
        The directory seeds are written to.
    """
    return config.virtualbox.template_ova.parent / "seeds"


def _deployer_public_key(config: ClusterConfig) -> str:
    """Read the public key that will be authorised on every guest.

    Args:
        config: The cluster configuration, providing the private key path.

    Returns:
        The OpenSSH public key line.

    Raises:
        VmDeployError: If the public key is missing or unreadable.
    """
    private_path = config.ssh.key_path
    public_path = private_path.with_suffix(private_path.suffix + ".pub")
    try:
        return public_path.read_text(encoding="ascii").strip()
    except OSError as error:
        raise VmDeployError(
            f"Cannot read the deployer public key at {public_path}: {error}. "
            "Guests take their only login from this key; run 'vmdeploy setup' to generate one."
        ) from error


def _wait_for_cloud_init(guest: RemoteHost, timeout_seconds: int) -> None:
    """Block until the guest has finished applying its seed.

    SSH answering only means sshd is up; cloud-init may still be creating the
    account and setting the hostname. Checking here turns a race into a clear
    failure, and surfaces the guest's own diagnosis when it did not finish.

    Args:
        guest: A connected SSH session to the guest.
        timeout_seconds: How long to wait for completion.

    Raises:
        VmDeployError: If cloud-init did not finish successfully.
    """
    result = guest.run(
        "cloud-init status --wait --long || true", check=False, timeout=timeout_seconds
    )
    report = result.stdout.strip()
    if "status: done" not in report:
        raise VmDeployError(
            f"cloud-init did not finish on {guest.address}; it reported:\n{report}\n"
            "The guest booted but may not have applied its seed."
        )
    _LOG.debug("cloud-init finished on %s", guest.address)


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
