"""Retrieval and deployment of the published static site.

Content is pulled directly by each backend guest with ``curl`` piped into
``tar`` rather than being downloaded to the automation host and pushed over
SFTP. A GitHub source tarball is tens of megabytes and SFTP moves it a chunk
at a time over an encrypted channel, so the guest-side pull is substantially
faster and keeps the automation host stateless.

Each backend is stamped with its own identity two ways: an ``X-Backend-Host``
response header emitted by ``mod_headers``, and a ``<meta name="x-backend-host">``
tag injected into every served HTML document. The header survives any page,
including static assets, and the meta tag is visible to the Playwright DOM, so
load balancer distribution can be proven from either layer independently.
"""

from __future__ import annotations

import logging
from typing import Final

from vmdeploy.config import WebsiteConfig
from vmdeploy.exceptions import RemoteCommandError, WebsiteFetchError
from vmdeploy.ssh_client import RemoteHost

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

BACKEND_HEADER: Final[str] = "X-Backend-Host"
BACKEND_META_NAME: Final[str] = "x-backend-host"

_STAGING_DIR: Final[str] = "/tmp/vmdeploy-site"
_DOWNLOAD_TIMEOUT_SECONDS: Final[int] = 600


def fetch_site_to_guest(host: RemoteHost, website: WebsiteConfig) -> str:
    """Download and unpack the latest site build inside a guest.

    Args:
        host: A connected SSH session to the backend guest.
        website: Static site source configuration.

    Returns:
        The absolute path of the unpacked site root inside the guest. GitHub
        source tarballs wrap everything in a single ``<repo>-<ref>`` directory,
        so the extracted top-level directory is resolved and returned rather
        than assumed.

    Raises:
        WebsiteFetchError: If the download, extraction, or root resolution
            fails.
    """
    _LOG.info("Fetching %s on %s", website.archive_url, host.address)
    host.run(f"rm -rf {_STAGING_DIR} && mkdir -p {_STAGING_DIR}")

    try:
        host.run(
            f"curl -fsSL --http1.1 --retry 5 --retry-delay 3 --retry-all-errors "
            f"--connect-timeout 30 {website.archive_url} | tar -xz -C {_STAGING_DIR}",
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        )
    except RemoteCommandError as exc:
        raise WebsiteFetchError(
            f"Could not download or unpack {website.archive_url} on {host.address}: {exc}"
        ) from exc

    listing = host.run(f"find {_STAGING_DIR} -mindepth 1 -maxdepth 1 -type d").lines()
    if len(listing) != 1:
        raise WebsiteFetchError(
            f"Expected exactly one top-level directory in the site archive on "
            f"{host.address}, found {len(listing)}: {listing}"
        )

    site_root = listing[0]
    index_present = host.run(f"test -f {site_root}/index.html", check=False)
    if not index_present.ok:
        raise WebsiteFetchError(
            f"Site archive unpacked to {site_root} on {host.address} but contains no "
            "index.html; the archive_url may point at the wrong repository or branch"
        )

    _LOG.info("Site unpacked to %s on %s", site_root, host.address)
    return site_root


def publish_site(host: RemoteHost, website: WebsiteConfig, backend_hostname: str) -> None:
    """Publish the fetched site into the guest's Apache document root.

    Args:
        host: A connected SSH session to the backend guest.
        website: Static site source configuration.
        backend_hostname: Identity stamped into every served HTML document.

    Raises:
        WebsiteFetchError: If the content cannot be fetched or installed.
    """
    site_root = fetch_site_to_guest(host, website)
    document_root = website.document_root

    _LOG.info("Publishing site to %s:%s", host.address, document_root)
    try:
        host.sudo(f"rm -rf {document_root}")
        host.sudo(f"mkdir -p {document_root}")
        # Trailing slash on the source copies contents, not the directory.
        host.sudo(f"cp -a {site_root}/. {document_root}/")
        host.sudo(f"chown -R www-data:www-data {document_root}")
        host.sudo(f"find {document_root} -type d -exec chmod 755 {{}} +")
        host.sudo(f"find {document_root} -type f -exec chmod 644 {{}} +")
    except RemoteCommandError as exc:
        raise WebsiteFetchError(
            f"Could not install site content into {document_root} on {host.address}: {exc}"
        ) from exc

    stamp_backend_identity(host, document_root, backend_hostname)
    host.run(f"rm -rf {_STAGING_DIR}", check=False)


def stamp_backend_identity(host: RemoteHost, document_root: str, backend_hostname: str) -> None:
    """Inject a backend identity meta tag into every served HTML document.

    The tag is inserted immediately after each document's ``<head>``. Files
    that already carry the marker are skipped, so republishing is idempotent
    and repeated runs cannot accumulate duplicate tags.

    Args:
        host: A connected SSH session to the backend guest.
        document_root: Apache document root inside the guest.
        backend_hostname: The value written into the meta tag.

    Raises:
        WebsiteFetchError: If the injection fails.
    """
    meta_tag = f'<meta name="{BACKEND_META_NAME}" content="{backend_hostname}">'
    _LOG.info("Stamping %s identity into HTML under %s", backend_hostname, document_root)

    # sed operates per file: skip any file already carrying the marker, then
    # insert the tag after the first <head> occurrence (case-insensitive).
    script = (
        f"for html_file in $(grep -rlI --include='*.html' -e '<head' {document_root} "
        f"2>/dev/null); do "
        f"  grep -q '{BACKEND_META_NAME}' \"$html_file\" && continue; "
        f"  sed -i '0,/<[Hh][Ee][Aa][Dd][^>]*>/s//&\\n    {meta_tag}/' \"$html_file\"; "
        f"done"
    )
    try:
        host.sudo(f"sh -c {_shell_quote(script)}")
    except RemoteCommandError as exc:
        raise WebsiteFetchError(
            f"Could not stamp backend identity into {document_root} on {host.address}: {exc}"
        ) from exc


def _shell_quote(value: str) -> str:
    """Quote a string for safe use as a single POSIX shell word.

    Args:
        value: The string to quote.

    Returns:
        The value wrapped in single quotes with embedded single quotes
        escaped, matching ``shlex.quote`` semantics for a POSIX guest. The
        stdlib version is not used because it targets the local platform,
        which is Windows here while the target shell is always POSIX.
    """
    return "'" + value.replace("'", "'\\''") + "'"
