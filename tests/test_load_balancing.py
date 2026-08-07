"""End-to-end validation of Apache load balancer distribution.

Distribution is proven at two independent layers. The HTTP layer reads the
``X-Backend-Host`` response header, which the balancer forwards untouched from
whichever member served the request. The browser layer reads the
``<meta name="x-backend-host">`` tag that provisioning stamps into every served
document, which proves the *rendered* page really came from that member rather
than from a proxy cache.

Both layers defeat connection reuse deliberately. Apache's ``byrequests``
scheduler assigns a member per TCP connection, so a client that keeps one
connection alive is pinned to one backend and would make correct balancing
indistinguishable from a dead pool.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Final

import allure
import pytest
import requests
from requests.exceptions import RequestException

from vmdeploy.website import BACKEND_META_NAME

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

REQUEST_COUNT: Final[int] = 24
REQUEST_TIMEOUT_SECONDS: Final[int] = 15
BROWSER_LOAD_COUNT: Final[int] = 8
# Round-robin over two members should approach 50/50. Ten percent is a wide
# floor that still fails a pool where one member is effectively dead, while
# tolerating a member that briefly retries.
MIN_SHARE: Final[float] = 0.10

_BACKEND_HEADER: Final[str] = "X-Backend-Host"


def _fetch_backend_identity(url: str, attempt: int) -> str:
    """Issue one non-reusable request and report which backend answered.

    Args:
        url: The load balancer base URL.
        attempt: Sequence number, used to defeat any intermediate cache.

    Returns:
        The value of the backend identity header, or an empty string if the
        header was absent.

    Raises:
        AssertionError: If the request fails or returns a non-200 status.
    """
    try:
        response = requests.get(
            f"{url}/?lb-probe={attempt}",
            timeout=REQUEST_TIMEOUT_SECONDS,
            # Close the connection so the next request is scheduled afresh.
            headers={"Connection": "close", "Cache-Control": "no-cache"},
        )
    except RequestException as exc:
        raise AssertionError(f"Request {attempt} to {url} failed: {exc}") from exc

    assert response.status_code == 200, (
        f"Request {attempt} to {url} returned HTTP {response.status_code}; "
        f"body starts: {response.text[:200]!r}"
    )
    return response.headers.get(_BACKEND_HEADER, "")


@allure.epic("Cluster Infrastructure")
@allure.feature("Load Balancing")
@allure.story("HTTP request distribution")
@pytest.mark.e2e
@pytest.mark.loadbalancing
class TestHttpDistribution:
    """The balancer spreads requests across every configured backend."""

    @allure.title("Every backend answers at least one of 24 requests")
    @allure.severity(allure.severity_level.CRITICAL)
    def test_all_backends_receive_traffic(
        self, live_balancer: str, backend_hostnames: tuple[str, ...]
    ) -> None:
        """Each configured backend must serve part of the request stream.

        Args:
            live_balancer: The reachable balancer base URL.
            backend_hostnames: Configured backend hostnames.
        """
        with allure.step(f"Issue {REQUEST_COUNT} requests to {live_balancer}"):
            observed = Counter(
                _fetch_backend_identity(live_balancer, attempt)
                for attempt in range(REQUEST_COUNT)
            )

        allure.attach(
            "\n".join(
                f"{name or '<no header>'}: {count}"
                for name, count in observed.most_common()
            ),
            name="Backend distribution",
            attachment_type=allure.attachment_type.TEXT,
        )
        _LOG.info("Observed distribution: %s", dict(observed))

        with allure.step("Assert the identity header was present on every response"):
            assert "" not in observed, (
                f"{observed['']} response(s) carried no {_BACKEND_HEADER} header; "
                "mod_headers may not be enabled on the backends"
            )

        with allure.step("Assert every configured backend served traffic"):
            silent = [name for name in backend_hostnames if observed[name] == 0]
            assert not silent, (
                f"Backend(s) {silent} served none of {REQUEST_COUNT} requests. "
                f"Observed: {dict(observed)}"
            )

    @allure.title("No single backend absorbs a disproportionate share")
    def test_distribution_is_not_skewed(
        self, live_balancer: str, backend_hostnames: tuple[str, ...]
    ) -> None:
        """Round-robin scheduling should keep shares broadly comparable.

        Args:
            live_balancer: The reachable balancer base URL.
            backend_hostnames: Configured backend hostnames.
        """
        observed = Counter(
            _fetch_backend_identity(live_balancer, attempt) for attempt in range(REQUEST_COUNT)
        )
        total = sum(observed.values())
        assert total, "no responses were collected"

        shares = {name: observed[name] / total for name in backend_hostnames}
        allure.attach(
            "\n".join(f"{name}: {share:.1%}" for name, share in shares.items()),
            name="Backend share",
            attachment_type=allure.attachment_type.TEXT,
        )

        starved = {name: share for name, share in shares.items() if share < MIN_SHARE}
        assert not starved, (
            f"Backend(s) received less than {MIN_SHARE:.0%} of traffic: "
            f"{ {name: f'{share:.1%}' for name, share in starved.items()} }"
        )

    @allure.title("Unknown backend identities are reported rather than ignored")
    def test_no_unexpected_backends(
        self, live_balancer: str, backend_hostnames: tuple[str, ...]
    ) -> None:
        """Only configured members may answer through the balancer.

        A stale ``BalancerMember`` left behind by a previous deployment would
        otherwise keep serving traffic unnoticed.

        Args:
            live_balancer: The reachable balancer base URL.
            backend_hostnames: Configured backend hostnames.
        """
        observed = {
            _fetch_backend_identity(live_balancer, attempt) for attempt in range(REQUEST_COUNT)
        }
        unexpected = observed - set(backend_hostnames) - {""}
        assert not unexpected, (
            f"Traffic was served by unconfigured backend(s): {sorted(unexpected)}"
        )


@allure.epic("Cluster Infrastructure")
@allure.feature("Load Balancing")
@allure.story("Served content integrity")
@pytest.mark.e2e
@pytest.mark.loadbalancing
class TestServedContent:
    """The balanced site serves the published static build correctly."""

    @allure.title("The balancer serves a non-trivial HTML document")
    def test_serves_html(self, live_balancer: str) -> None:
        """The document root must contain the published site, not a default page.

        Args:
            live_balancer: The reachable balancer base URL.
        """
        try:
            response = requests.get(live_balancer, timeout=REQUEST_TIMEOUT_SECONDS)
        except RequestException as exc:
            pytest.fail(f"Could not fetch {live_balancer}: {exc}")

        assert response.status_code == 200
        body = response.text.lower()

        with allure.step("Assert the response is HTML"):
            assert "<html" in body, f"response is not an HTML document: {response.text[:200]!r}"

        with allure.step("Assert the Apache default placeholder is not being served"):
            assert "apache2 ubuntu default page" not in body, (
                "the balancer is serving Apache's default page; site content was not published"
            )

    @allure.title("Both backends serve byte-identical content")
    def test_backends_serve_identical_content(
        self, live_balancer: str, backend_hostnames: tuple[str, ...]
    ) -> None:
        """Members must be interchangeable, or users see different pages per hit.

        Args:
            live_balancer: The reachable balancer base URL.
            backend_hostnames: Configured backend hostnames.
        """
        bodies: dict[str, str] = {}
        for attempt in range(REQUEST_COUNT):
            try:
                response = requests.get(
                    f"{live_balancer}/?content-probe={attempt}",
                    timeout=REQUEST_TIMEOUT_SECONDS,
                    headers={"Connection": "close"},
                )
            except RequestException as exc:
                pytest.fail(f"Content probe {attempt} failed: {exc}")

            backend = response.headers.get(_BACKEND_HEADER, "")
            if backend and backend not in bodies:
                # Strip the per-host identity stamp before comparing, since it
                # is the one line that is expected to differ.
                bodies[backend] = "\n".join(
                    line for line in response.text.splitlines()
                    if BACKEND_META_NAME not in line
                )
            if len(bodies) == len(backend_hostnames):
                break

        assert len(bodies) >= 2, (
            f"Only reached {sorted(bodies)}; cannot compare content across backends"
        )

        reference_host, reference_body = next(iter(bodies.items()))
        for host, body in bodies.items():
            if host == reference_host:
                continue
            assert body == reference_body, (
                f"{host} serves different content than {reference_host}; "
                "the backends are out of sync"
            )


@allure.epic("Cluster Infrastructure")
@allure.feature("Load Balancing")
@allure.story("Browser-level distribution")
@pytest.mark.e2e
@pytest.mark.loadbalancing
@pytest.mark.slow
class TestBrowserDistribution:
    """A real browser observes traffic reaching more than one backend."""

    @allure.title("Repeated browser loads reach more than one backend")
    @allure.severity(allure.severity_level.CRITICAL)
    def test_browser_sees_multiple_backends(
        self,
        page: object,
        live_balancer: str,
        backend_hostnames: tuple[str, ...],
        js_errors: list[str],
    ) -> None:
        """Load the site repeatedly and collect the rendered identity stamps.

        Args:
            page: The Playwright page fixture.
            live_balancer: The reachable balancer base URL.
            backend_hostnames: Configured backend hostnames.
            js_errors: Accumulated uncaught JavaScript errors.
        """
        observed: Counter[str] = Counter()

        for attempt in range(BROWSER_LOAD_COUNT):
            identity = _load_and_read_identity(page, live_balancer, attempt)
            if identity:
                observed[identity] += 1

        allure.attach(
            "\n".join(f"{name}: {count}" for name, count in observed.most_common()),
            name="Browser-observed backends",
            attachment_type=allure.attachment_type.TEXT,
        )

        with allure.step("Assert the page raised no uncaught JavaScript errors"):
            assert not js_errors, f"page raised JavaScript errors: {js_errors}"

        with allure.step("Assert at least one identity stamp was rendered"):
            assert observed, (
                "no backend identity was found in any rendered page; the "
                f"<meta name='{BACKEND_META_NAME}'> stamp may be missing"
            )

        with allure.step("Assert the browser reached more than one backend"):
            assert len(observed) > 1, (
                f"all {BROWSER_LOAD_COUNT} browser loads hit a single backend "
                f"({sorted(observed)}); expected traffic across {list(backend_hostnames)}"
            )


def _load_and_read_identity(page: object, url: str, attempt: int) -> str:
    """Navigate to the site and read the rendered backend identity stamp.

    Args:
        page: The Playwright page fixture.
        url: The load balancer base URL.
        attempt: Sequence number, used to defeat the browser cache.

    Returns:
        The identity stamped into the rendered document, or an empty string if
        the page did not render one.

    Raises:
        AssertionError: If navigation fails outright.
    """
    goto = getattr(page, "goto", None)
    query = getattr(page, "get_attribute", None)
    wait = getattr(page, "wait_for_load_state", None)
    if not callable(goto) or not callable(query):
        raise AssertionError("Playwright page fixture is missing goto/get_attribute")

    try:
        response = goto(f"{url}/?browser-probe={attempt}", wait_until="domcontentloaded")
    # pylint: disable-next=broad-exception-caught
    except Exception as exc:  # Playwright raises a broad Error hierarchy.
        raise AssertionError(f"Browser navigation {attempt} to {url} failed: {exc}") from exc

    status = getattr(response, "status", None)
    if isinstance(status, int):
        assert status == 200, f"browser load {attempt} returned HTTP {status}"

    # Static pages settle at domcontentloaded, but a script-driven page may
    # still be mutating the DOM; give the network a moment before reading.
    if callable(wait):
        try:
            wait("networkidle", timeout=5000)
        # pylint: disable-next=broad-exception-caught
        except Exception:  # A page with polling legitimately never goes idle.
            _LOG.debug("Load %d never reached networkidle; reading current DOM", attempt)

    try:
        value = query(f"meta[name='{BACKEND_META_NAME}']", "content")
    # pylint: disable-next=broad-exception-caught
    except Exception as exc:  # A selector error must not abort the whole sweep.
        _LOG.warning("Could not read identity meta tag on load %d: %s", attempt, exc)
        return ""

    return str(value) if value else ""
