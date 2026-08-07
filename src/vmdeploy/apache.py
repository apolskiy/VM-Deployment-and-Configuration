"""Apache configuration for the jump station balancer and backend servers.

The jump station runs ``mod_proxy_balancer`` with the ``byrequests`` scheduler
and no sticky-session directive, because the test suite proves distribution by
issuing many requests from one client and asserting that both members answer.
Any stickiness would pin that client to a single member and make correct
balancing indistinguishable from a broken pool.

Backends listen on a non-default port and stamp an ``X-Backend-Host`` header
onto every response, which is what lets the balancer's routing be observed
from outside without inspecting the balancer's own state.
"""

from __future__ import annotations

import logging
from typing import Final, Sequence

from vmdeploy.config import ClusterConfig, HostConfig
from vmdeploy.exceptions import VmDeployError
from vmdeploy.ssh_client import RemoteHost
from vmdeploy.website import BACKEND_HEADER

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

BALANCER_NAME: Final[str] = "apcluster"
_SITE_CONF: Final[str] = "/etc/apache2/sites-available/000-default.conf"
_PORTS_CONF: Final[str] = "/etc/apache2/ports.conf"
_APT_TIMEOUT_SECONDS: Final[int] = 900

_BALANCER_MODULES: Final[tuple[str, ...]] = (
    "proxy",
    "proxy_http",
    "proxy_balancer",
    "lbmethod_byrequests",
    "headers",
    "status",
)
_BACKEND_MODULES: Final[tuple[str, ...]] = ("headers",)


def install_apache(host: RemoteHost) -> None:
    """Install the Apache web server if it is not already present.

    Args:
        host: A connected SSH session to the target guest.

    Raises:
        RemoteCommandError: If package installation fails.
    """
    if host.run("command -v apache2", check=False).ok:
        _LOG.info("Apache already installed on %s", host.address)
        return

    _LOG.info("Installing Apache on %s", host.address)
    host.sudo("apt-get update -qq", timeout=_APT_TIMEOUT_SECONDS)
    host.sudo(
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq apache2",
        timeout=_APT_TIMEOUT_SECONDS,
    )


def enable_modules(host: RemoteHost, modules: Sequence[str]) -> None:
    """Enable Apache modules, skipping any already active.

    Args:
        host: A connected SSH session to the target guest.
        modules: Module names to enable, without the ``mod_`` prefix.

    Raises:
        RemoteCommandError: If a module cannot be enabled.
    """
    for module in modules:
        _LOG.debug("Enabling Apache module %s on %s", module, host.address)
        host.sudo(f"a2enmod -q {module}")


def restart_apache(host: RemoteHost) -> None:
    """Validate the Apache configuration and restart the service.

    The configuration is checked with ``apachectl configtest`` first. Without
    that gate a malformed directive takes the service down and the failure
    surfaces later as an unrelated connection error during testing.

    Args:
        host: A connected SSH session to the target guest.

    Raises:
        VmDeployError: If the configuration is invalid or the service fails
            to come back up.
    """
    check = host.sudo("apachectl configtest", check=False)
    if not check.ok:
        raise VmDeployError(
            f"Apache configuration is invalid on {host.address}; refusing to restart.\n"
            f"{check.stderr.strip() or check.stdout.strip()}"
        )

    _LOG.info("Restarting Apache on %s", host.address)
    host.sudo("systemctl restart apache2")
    host.sudo("systemctl enable apache2", check=False)

    active = host.run("systemctl is-active apache2", check=False)
    if active.stdout.strip() != "active":
        status = host.sudo("systemctl status apache2 --no-pager -l", check=False)
        raise VmDeployError(
            f"Apache did not return to active state on {host.address}: "
            f"{active.stdout.strip() or 'unknown'}\n{status.stdout.strip()}"
        )


def render_backend_vhost(backend: HostConfig, document_root: str) -> str:
    """Build the Apache virtual host for a backend web server.

    Args:
        backend: The backend host being configured.
        document_root: Directory served by this virtual host.

    Returns:
        The complete virtual host configuration file contents.
    """
    return f"""# Managed by vmdeploy. Manual edits are overwritten on redeploy.
<VirtualHost *:{backend.http_port}>
    ServerName {backend.hostname}
    DocumentRoot {document_root}

    <Directory {document_root}>
        Options -Indexes +FollowSymLinks
        AllowOverride None
        Require all granted
    </Directory>

    # Identity stamp consumed by the load balancing test suite.
    Header always set {BACKEND_HEADER} "{backend.hostname}"

    ErrorLog ${{APACHE_LOG_DIR}}/{backend.hostname}-error.log
    CustomLog ${{APACHE_LOG_DIR}}/{backend.hostname}-access.log combined
</VirtualHost>
"""


def render_balancer_vhost(config: ClusterConfig, members: dict[str, str]) -> str:
    """Build the Apache virtual host for the jump station load balancer.

    Args:
        config: The cluster configuration.
        members: Mapping of backend hostname to reachable IPv4 address.

    Returns:
        The complete virtual host configuration file contents.

    Raises:
        VmDeployError: If any configured backend has no resolved address,
            which would otherwise produce a balancer pool that silently omits
            a member.
    """
    lines: list[str] = []
    for backend in config.backends:
        address = members.get(backend.hostname, "")
        if not address:
            raise VmDeployError(
                f"No resolved address for backend {backend.hostname}; "
                "cannot build a complete balancer pool"
            )
        lines.append(
            f'        BalancerMember "http://{address}:{backend.http_port}" '
            f"route={backend.hostname}"
        )
    member_block = "\n".join(lines)

    return f"""# Managed by vmdeploy. Manual edits are overwritten on redeploy.
<VirtualHost *:{config.jump.http_port}>
    ServerName {config.jump.hostname}

    ProxyRequests Off
    ProxyPreserveHost On

    <Proxy "balancer://{BALANCER_NAME}">
{member_block}
        # byrequests with no stickysession: consecutive requests from one
        # client must land on different members for the suite to pass.
        ProxySet lbmethod=byrequests
    </Proxy>

    # Exclude the manager endpoint from proxying so it is served locally.
    ProxyPass        "/balancer-manager" "!"
    ProxyPass        "/" "balancer://{BALANCER_NAME}/"
    ProxyPassReverse "/" "balancer://{BALANCER_NAME}/"

    <Location "/balancer-manager">
        SetHandler balancer-manager
        Require local
        Require ip 192.168.0.0/16
    </Location>

    ErrorLog ${{APACHE_LOG_DIR}}/{config.jump.hostname}-error.log
    CustomLog ${{APACHE_LOG_DIR}}/{config.jump.hostname}-access.log combined
</VirtualHost>
"""


def configure_backend(host: RemoteHost, backend: HostConfig, document_root: str) -> None:
    """Install and configure Apache as a static web server.

    Args:
        host: A connected SSH session to the backend guest.
        backend: The backend host definition.
        document_root: Directory this backend serves.

    Raises:
        VmDeployError: If Apache cannot be configured or restarted.
    """
    _LOG.info("Configuring %s as a backend web server", backend.hostname)
    install_apache(host)
    enable_modules(host, _BACKEND_MODULES)
    _ensure_listen_port(host, backend.http_port)
    host.put_bytes(
        render_backend_vhost(backend, document_root).encode("utf-8"), _SITE_CONF
    )
    restart_apache(host)


def configure_balancer(host: RemoteHost, config: ClusterConfig, members: dict[str, str]) -> None:
    """Install and configure Apache as the cluster load balancer.

    Args:
        host: A connected SSH session to the jump station.
        config: The cluster configuration.
        members: Mapping of backend hostname to reachable IPv4 address.

    Raises:
        VmDeployError: If Apache cannot be configured or restarted.
    """
    _LOG.info("Configuring %s as the load balancer", config.jump.hostname)
    install_apache(host)
    enable_modules(host, _BALANCER_MODULES)
    _ensure_listen_port(host, config.jump.http_port)
    host.put_bytes(
        render_balancer_vhost(config, members).encode("utf-8"), _SITE_CONF
    )
    restart_apache(host)


def _ensure_listen_port(host: RemoteHost, port: int) -> None:
    """Ensure Apache's ports.conf contains a Listen directive for a port.

    Args:
        host: A connected SSH session to the target guest.
        port: The TCP port Apache must listen on.

    Raises:
        RemoteCommandError: If ports.conf cannot be read or updated.
    """
    try:
        current = host.get_bytes(_PORTS_CONF).decode("utf-8", errors="replace")
    except VmDeployError:
        current = ""

    directives = {
        line.strip().split()[1]
        for line in current.splitlines()
        if line.strip().startswith("Listen") and len(line.strip().split()) > 1
    }
    if str(port) in directives:
        _LOG.debug("Apache on %s already listens on %d", host.address, port)
        return

    _LOG.info("Adding Listen %d to %s on %s", port, _PORTS_CONF, host.address)
    host.sudo(f"sh -c 'echo \"Listen {port}\" >> {_PORTS_CONF}'")
