"""Command line orchestration for cluster deployment.

Subcommands are separable on purpose. Exporting a multi-gigabyte golden
appliance takes far longer than reconfiguring Apache, so an operator iterating
on balancer configuration can re-run ``configure`` alone instead of rebuilding
the whole cluster.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from vmdeploy.apache import configure_backend, configure_balancer
from vmdeploy.config import ClusterConfig, load_cluster_config
from vmdeploy.exceptions import InventoryError, VmDeployError
from vmdeploy.inventory_service import (
    build_records,
    deploy_service,
    enrich_with_ipv6,
    mark_manifest_removed,
    seed_inventory,
)
from vmdeploy.preflight import (
    CheckStatus,
    format_report,
    run_preflight,
    worst_status,
)
from vmdeploy.provision import (
    build_golden_template,
    cluster_addresses,
    export_golden_template,
    provision_cluster,
    teardown_cluster,
)
from vmdeploy.setup import (
    DEFAULT_ADMIN_USER,
    restore_bootstrap,
    run_keys_only_setup,
    run_setup,
)
from vmdeploy.ssh_client import RemoteHost
from vmdeploy.virtualbox import VBoxManage
from vmdeploy.website import publish_site

_LOG: Final[logging.Logger] = logging.getLogger("vmdeploy")

_DEFAULT_CONFIG: Final[Path] = Path("config/cluster.toml")
_GOSERVICE_DIR: Final[Path] = Path("goservice")

EXIT_OK: Final[int] = 0
EXIT_FAILURE: Final[int] = 1
EXIT_INTERRUPTED: Final[int] = 130


def configure_logging(verbose: bool) -> None:
    """Configure root logging for command line use.

    Args:
        verbose: Whether to emit debug-level records.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Paramiko logs every packet at DEBUG, which drowns out provisioning output.
    logging.getLogger("paramiko").setLevel(logging.WARNING)


def cmd_template(config: ClusterConfig, args: argparse.Namespace) -> int:
    """Build the golden OVA: bake dependencies into the template, then export.

    Args:
        config: The cluster configuration.
        args: Parsed arguments, providing ``export_only`` and the Go source
            directory.

    Returns:
        A process exit status.
    """
    vbox = VBoxManage(config.virtualbox.vboxmanage)
    # 'deploy' reuses one namespace for every step, so --export-only is only
    # present when 'template' was invoked directly.
    if getattr(args, "export_only", False):
        _LOG.info("Exporting the template as-is (--export-only); dependencies not baked")
        export_golden_template(vbox, config)
    else:
        build_golden_template(vbox, config, args.goservice_dir)
    _LOG.info("Golden template ready at %s", config.virtualbox.template_ova)
    return EXIT_OK


def cmd_provision(config: ClusterConfig, _args: argparse.Namespace) -> int:
    """Import and boot every cluster guest from the golden template.

    Args:
        config: The cluster configuration.
        _args: Parsed arguments, unused by this subcommand.

    Returns:
        A process exit status.
    """
    vbox = VBoxManage(config.virtualbox.vboxmanage)
    addresses = provision_cluster(vbox, config)
    _report_addresses(addresses)
    return EXIT_OK


def cmd_configure(config: ClusterConfig, args: argparse.Namespace) -> int:
    """Configure Apache on every host and deploy the inventory service.

    Args:
        config: The cluster configuration.
        args: Parsed arguments, providing the Go source directory.

    Returns:
        A process exit status.

    Raises:
        VmDeployError: If any host is unreachable or misconfigured.
    """
    vbox = VBoxManage(config.virtualbox.vboxmanage)
    addresses = cluster_addresses(vbox, config)

    missing = [host.hostname for host in config.all_hosts if host.hostname not in addresses]
    if missing:
        raise VmDeployError(
            f"Cannot configure the cluster; no address for {missing}. "
            "Run 'vmdeploy provision' first."
        )

    for backend in config.backends:
        address = addresses[backend.hostname]
        _LOG.info("Configuring backend %s at %s", backend.hostname, address)
        with RemoteHost(address, config.ssh) as guest:
            configure_backend(guest, backend, config.website.document_root)
            publish_site(guest, config.website, backend.hostname)

    jump_address = addresses[config.jump.hostname]
    _LOG.info("Configuring jump station %s at %s", config.jump.hostname, jump_address)
    with RemoteHost(jump_address, config.ssh) as jump:
        configure_balancer(jump, config, addresses)
        deploy_service(jump, config, args.goservice_dir)
        ipv6 = enrich_with_ipv6(config, addresses)
        seed_inventory(jump, config, build_records(config, addresses, ipv6))

    _report_addresses(addresses)
    _print_endpoints(config, jump_address)
    return EXIT_OK


def cmd_deploy(config: ClusterConfig, args: argparse.Namespace) -> int:
    """Run the full pipeline: export, provision, then configure.

    Args:
        config: The cluster configuration.
        args: Parsed arguments.

    Returns:
        A process exit status.
    """
    if not getattr(args, "skip_preflight", False):
        results = run_preflight(config)
        print("Preflight checks:")
        print(format_report(results))
        if worst_status(results) is CheckStatus.FAIL:
            _LOG.error(
                "Preflight FAILED; refusing to deploy. Fix the failures above, or pass "
                "--skip-preflight to override at your own risk."
            )
            return EXIT_FAILURE

    for step in (cmd_template, cmd_provision, cmd_configure):
        status = step(config, args)
        if status != EXIT_OK:
            return status
    return EXIT_OK


def cmd_status(config: ClusterConfig, _args: argparse.Namespace) -> int:
    """Report the state and address of every cluster guest.

    Args:
        config: The cluster configuration.
        _args: Parsed arguments, unused by this subcommand.

    Returns:
        A process exit status. Non-zero if any guest is not running.
    """
    vbox = VBoxManage(config.virtualbox.vboxmanage)
    addresses = cluster_addresses(vbox, config)

    print(f"{'HOSTNAME':<16}{'ROLE':<10}{'VM STATE':<14}{'ADDRESS':<16}")
    all_running = True
    for host in config.all_hosts:
        state = vbox.state(host.vm_name)
        address = addresses.get(host.hostname, "-")
        if state.value != "running":
            all_running = False
        print(f"{host.hostname:<16}{host.role.value:<10}{state.value:<14}{address:<16}")

    _print_endpoints(config, addresses.get(config.jump.hostname))
    return EXIT_OK if all_running else EXIT_FAILURE


def _print_endpoints(config: ClusterConfig, jump_address: str | None) -> None:
    """Print the URLs an operator uses to reach the live cluster.

    Hostnames are used rather than the DHCP-assigned address, because the
    address changes across reboots while the hostname the DHCP server registers
    does not. The resolved address is shown once for reference.

    Args:
        config: The cluster configuration.
        jump_address: The jump station's resolved address, if known.
    """
    host = config.jump.hostname
    jump_port = "" if config.jump.http_port == 80 else f":{config.jump.http_port}"
    inventory_base = f"http://{host}:{config.inventory.service_port}"
    print()
    print("Endpoints (use hostnames; the DHCP address changes across reboots):")
    print(f"  Load balancer      http://{host}{jump_port}/")
    print(f"  Inventory (HTML)   {inventory_base}/clusterview")
    print(f"  Inventory (JSON)   {inventory_base}/api/inventory")
    print("  Local manifest     scripts/manifest.ps1 show")
    if jump_address:
        print(f"  ({host} currently resolves to {jump_address})")
    port = config.inventory.service_port
    print()
    print("  Remote access from off the jump station:")
    print(f"    curl http://{host}:{port}/api/inventory")
    print(f"    # or tunnel over SSH, then browse http://localhost:{port}/clusterview :")
    print(f"    ssh -L {port}:localhost:{port} {config.ssh.user}@{host}")


def cmd_setup(config: ClusterConfig, args: argparse.Namespace) -> int:
    """Generate credentials, create the hardened user, and disable the stock one.

    Args:
        config: The cluster configuration (its ssh section is the bootstrap
            identity used once to create the new user).
        args: Parsed arguments, providing the new user name and key path.

    Returns:
        A process exit status.
    """
    setup_action = run_keys_only_setup if args.keys_only else run_setup
    setup_action(
        config,
        args.config,
        new_user=args.new_user,
        new_key_path=Path(args.new_key).expanduser(),
    )
    return EXIT_OK


def cmd_restore_bootstrap(config: ClusterConfig, args: argparse.Namespace) -> int:
    """Re-enable a disabled account on the template box after image export.

    Args:
        config: The cluster configuration.
        args: Parsed arguments, providing the user, its public key, and whether
            to leave the template running.

    Returns:
        A process exit status.

    Raises:
        VmDeployError: If the public key cannot be read.
    """
    key_source = Path(args.pubkey_file).expanduser()
    try:
        public_key = key_source.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise VmDeployError(f"Cannot read public key from {key_source}: {exc}") from exc

    restore_bootstrap(
        config,
        username=args.user,
        public_key=public_key,
        leave_running=args.leave_running,
    )
    return EXIT_OK


def cmd_preflight(config: ClusterConfig, _args: argparse.Namespace) -> int:
    """Report whether the host is ready to deploy or tear down the cluster.

    Args:
        config: The cluster configuration.
        _args: Parsed arguments, unused by this subcommand.

    Returns:
        EXIT_OK if no check failed (warnings are allowed), else EXIT_FAILURE.
    """
    results = run_preflight(config)
    print("Preflight checks:")
    print(format_report(results))

    outcome = worst_status(results)
    if outcome is CheckStatus.FAIL:
        _LOG.error("Preflight FAILED; resolve the failures above before deploying")
        return EXIT_FAILURE
    if outcome is CheckStatus.WARN:
        _LOG.warning("Preflight passed with warnings; review them before proceeding")
    else:
        _LOG.info("Preflight passed")
    return EXIT_OK


def cmd_teardown(config: ClusterConfig, args: argparse.Namespace) -> int:
    """Destroy every cluster guest, leaving the golden template intact.

    Args:
        config: The cluster configuration.
        args: Parsed arguments, providing the confirmation flag.

    Returns:
        A process exit status.
    """
    targets = [host.vm_name for host in config.all_hosts]
    if not args.yes:
        print(f"This permanently deletes {len(targets)} VM(s) and their disks: {targets}")
        print("Re-run with --yes to proceed.")
        return EXIT_FAILURE

    vbox = VBoxManage(config.virtualbox.vboxmanage)
    teardown_cluster(vbox, config)

    # Update the manifest exactly as a deploy does, so the durable record
    # reflects that the cluster was torn down rather than silently going stale.
    # The live copy on the jump station is gone with the VM, so the canonical
    # local manifest is the only place this can be recorded.
    try:
        removed = mark_manifest_removed(config)
        _LOG.info("Manifest updated: %d host(s) marked Removed/Inactive", len(removed))
    except InventoryError as exc:
        _LOG.warning("Cluster destroyed, but the manifest could not be updated: %s", exc)

    _LOG.info("Cluster torn down")
    return EXIT_OK


def _report_addresses(addresses: dict[str, str]) -> None:
    """Log the resolved address of each host.

    Args:
        addresses: Mapping of hostname to IPv4 address.
    """
    for hostname, address in sorted(addresses.items()):
        _LOG.info("  %-16s %s", hostname, address)


def build_parser() -> argparse.ArgumentParser:
    """Build the command line argument parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="vmdeploy",
        description="Provision and configure a VirtualBox Apache load balancing cluster.",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help=f"path to the cluster TOML configuration (default: {_DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--goservice-dir",
        type=Path,
        default=_GOSERVICE_DIR,
        help=f"directory holding the Go inventory service sources (default: {_GOSERVICE_DIR})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")

    subparsers = parser.add_subparsers(dest="command", required=True)
    template = subparsers.add_parser(
        "template", help="build the golden OVA: bake deps into the template, then export"
    )
    template.add_argument(
        "--export-only",
        action="store_true",
        help="export the template as-is without baking dependencies",
    )
    setup = subparsers.add_parser(
        "setup",
        help="generate keys, create a hardened user on the template, disable the stock login",
    )
    setup.add_argument(
        "--new-user",
        default=DEFAULT_ADMIN_USER,
        help=f"operational account to create (default: {DEFAULT_ADMIN_USER})",
    )
    setup.add_argument(
        "--new-key",
        default="~/.vmdeploy/keys/vm_key",
        help="local path for the generated private key (default: ~/.vmdeploy/keys/vm_key)",
    )
    setup.add_argument(
        "--keys-only",
        action="store_true",
        help="generate local keys and stop; use this when deploying a pulled golden "
        "image, where there is no template machine to harden",
    )
    restore = subparsers.add_parser(
        "restore-bootstrap",
        help="re-enable a disabled account on the template box (image stays hardened)",
    )
    restore.add_argument("--user", required=True, help="the account to re-enable")
    restore.add_argument(
        "--pubkey-file", required=True, help="path to the account's OpenSSH public key"
    )
    restore.add_argument(
        "--leave-running", action="store_true", help="leave the template powered on afterward"
    )
    subparsers.add_parser("provision", help="import and boot the cluster guests")
    subparsers.add_parser("configure", help="configure Apache and the inventory service")
    subparsers.add_parser(
        "preflight", help="check host RAM, disk, tools, and keys before deploying"
    )
    deploy = subparsers.add_parser(
        "deploy", help="run preflight, template, provision, and configure in order"
    )
    deploy.add_argument(
        "--export-only",
        action="store_true",
        help="export the template as-is without baking dependencies",
    )
    deploy.add_argument(
        "--skip-preflight",
        action="store_true",
        help="skip the pre-deployment resource check (not recommended)",
    )
    subparsers.add_parser("status", help="report cluster VM state, addresses, and endpoints")

    teardown = subparsers.add_parser("teardown", help="destroy the cluster guests")
    teardown.add_argument("--yes", action="store_true", help="confirm destruction")

    return parser


_Handler = Callable[[ClusterConfig, argparse.Namespace], int]

_COMMANDS: Final[dict[str, _Handler]] = {
    "setup": cmd_setup,
    "restore-bootstrap": cmd_restore_bootstrap,
    "template": cmd_template,
    "provision": cmd_provision,
    "configure": cmd_configure,
    "preflight": cmd_preflight,
    "deploy": cmd_deploy,
    "status": cmd_status,
    "teardown": cmd_teardown,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``vmdeploy`` command.

    Args:
        argv: Argument vector, defaulting to ``sys.argv[1:]``.

    Returns:
        A process exit status.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    handler = _COMMANDS.get(args.command)
    if handler is None:
        # argparse.error exits the process; it never returns.
        parser.error(f"unknown command: {args.command}")

    try:
        config = load_cluster_config(args.config)
        return handler(config, args)
    except VmDeployError as exc:
        _LOG.error("%s", exc)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        _LOG.warning("Interrupted; the cluster may be in a partial state")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
