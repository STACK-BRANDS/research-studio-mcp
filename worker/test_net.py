"""Tests for worker/net.py: the SSRF-safe network boundary extracted from
worker.ingest (must still block private IPs exactly as before), plus
`safe_fetch_text()` used by the web.fetch connector, plus the DNS-rebinding
TOCTOU fix (FIX 1): every fetch now resolves a hostname EXACTLY ONCE and
PINS the actual connection to that resolved IP via a custom `HTTPAdapter`
(`_PinnedHostAdapter`), rather than validating a hostname and then handing
that same hostname back to `requests`/urllib3 to resolve (and connect to)
all over again.

No real network calls: DNS resolution (`socket.getaddrinfo`) is
monkeypatched, and the actual HTTP transport is monkeypatched at
`requests.Session.get` (NOT the bare `requests.get` function -- the pinned
fetch path constructs its own `requests.Session` per request so it can
mount `_PinnedHostAdapter` on it; `requests.get()` itself is never called
by this module anymore).
"""
import socket as socket_mod

import requests

from worker import net
from worker.net import (
    ALLOWED_SCHEMES,
    MAX_IMAGE_BYTES,
    USER_AGENT,
    _is_public_ip,
    _safe_download,
    _validate_url_host,
    canonical_host,
    fetch_robots_txt,
    safe_fetch_text,
)


# ---------------------------------------------------------------------------
# Backward-compat re-export: worker.ingest must still expose every one of
# these names unchanged after the extraction (no behavior change for its
# existing callers/tests).
# ---------------------------------------------------------------------------

def test_ingest_reexports_net_names_unchanged():
    from worker import ingest

    assert ingest.MAX_IMAGE_BYTES == MAX_IMAGE_BYTES
    assert ingest.ALLOWED_SCHEMES == ALLOWED_SCHEMES
    assert ingest._is_public_ip is _is_public_ip
    assert ingest._validate_url_host is _validate_url_host
    assert ingest._safe_download is _safe_download


# ---------------------------------------------------------------------------
# _is_public_ip -- SSRF must still block private/loopback/link-local/
# reserved/multicast/unspecified addresses, exactly as before the move.
# ---------------------------------------------------------------------------

def test_is_public_ip_true_for_public_addresses():
    assert _is_public_ip("8.8.8.8") is True
    assert _is_public_ip("1.1.1.1") is True


def test_is_public_ip_false_for_private_loopback_linklocal_reserved_multicast_unspecified():
    assert _is_public_ip("10.0.0.1") is False       # private
    assert _is_public_ip("192.168.1.1") is False    # private
    assert _is_public_ip("127.0.0.1") is False       # loopback
    assert _is_public_ip("169.254.169.254") is False  # link-local / cloud metadata
    assert _is_public_ip("240.0.0.1") is False       # reserved
    assert _is_public_ip("224.0.0.1") is False       # multicast
    assert _is_public_ip("0.0.0.0") is False         # unspecified


def test_is_public_ip_false_for_unparseable_string():
    assert _is_public_ip("not-an-ip") is False


def test_is_public_ip_blocks_ipv6_loopback_and_link_local():
    assert _is_public_ip("::1") is False
    assert _is_public_ip("fe80::1") is False


# ---------------------------------------------------------------------------
# _validate_url_host -- scheme allowlist + DNS-resolved public-IP-only check
# (bool-only, back-compat predicate -- see its docstring: the pinned fetch
# path below does NOT call this; it resolves via `_resolve_pinned_ip`
# itself so a fetch never resolves a hostname twice).
# ---------------------------------------------------------------------------

def test_validate_url_host_rejects_disallowed_scheme():
    assert _validate_url_host("ftp://example.com/file") is False


def test_validate_url_host_rejects_missing_hostname():
    assert _validate_url_host("http:///no-host") is False


def test_validate_url_host_rejects_dns_rebinding_to_private_ip(monkeypatch):
    """A hostname that LOOKS public but resolves to a private/loopback
    address (DNS rebinding) must be rejected -- this is the actual SSRF
    check, not just a scheme allowlist."""
    monkeypatch.setattr(
        net.socket, "getaddrinfo",
        lambda host, port: [(None, None, None, None, ("127.0.0.1", 0))],
    )
    assert _validate_url_host("http://evil.example.com/") is False


def test_validate_url_host_accepts_public_resolving_host(monkeypatch):
    monkeypatch.setattr(
        net.socket, "getaddrinfo",
        lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))],
    )
    assert _validate_url_host("https://example.com/") is True


def test_validate_url_host_rejects_when_any_resolved_address_is_private(monkeypatch):
    """Multiple A/AAAA records: even one private address among them fails
    the whole host (all-public-or-reject, not any-public-is-enough)."""
    monkeypatch.setattr(
        net.socket, "getaddrinfo",
        lambda host, port: [
            (None, None, None, None, ("93.184.216.34", 0)),
            (None, None, None, None, ("10.0.0.5", 0)),
        ],
    )
    assert _validate_url_host("https://example.com/") is False


def test_validate_url_host_rejects_dns_failure(monkeypatch):
    def _boom(host, port):
        raise socket_mod.gaierror("name resolution failed")

    monkeypatch.setattr(net.socket, "getaddrinfo", _boom)
    assert _validate_url_host("https://no-such-host.invalid/") is False


# ---------------------------------------------------------------------------
# _resolve_pinned_ip -- the single DNS-resolution call site behind every
# fetch (and behind `_validate_url_host`'s bool-only check above). Its
# return value is the exact IP `_PinnedHostAdapter` pins the connection to.
# ---------------------------------------------------------------------------

def test_resolve_pinned_ip_returns_first_ip_when_all_public(monkeypatch):
    monkeypatch.setattr(
        net.socket, "getaddrinfo",
        lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))],
    )
    assert net._resolve_pinned_ip("example.com") == "93.184.216.34"


def test_resolve_pinned_ip_none_when_any_address_private(monkeypatch):
    monkeypatch.setattr(
        net.socket, "getaddrinfo",
        lambda host, port: [
            (None, None, None, None, ("93.184.216.34", 0)),
            (None, None, None, None, ("10.0.0.5", 0)),
        ],
    )
    assert net._resolve_pinned_ip("example.com") is None


def test_resolve_pinned_ip_none_on_dns_failure(monkeypatch):
    def _boom(host, port):
        raise socket_mod.gaierror("nope")

    monkeypatch.setattr(net.socket, "getaddrinfo", _boom)
    assert net._resolve_pinned_ip("no-such-host.invalid") is None


def test_resolve_pinned_ip_none_on_empty_dns_result(monkeypatch):
    monkeypatch.setattr(net.socket, "getaddrinfo", lambda host, port: [])
    assert net._resolve_pinned_ip("example.com") is None


# ---------------------------------------------------------------------------
# THE core proof FIX 1 exists for: a DNS-rebinding attacker's server can
# answer differently on separate lookups. This module must never give it
# the chance -- exactly ONE resolution happens per fetch, and the
# connection is pinned to whatever that one lookup returned.
# ---------------------------------------------------------------------------

def test_dns_rebind_toctou_closed_by_single_resolution(monkeypatch):
    """getaddrinfo answers PUBLIC on the first (and, this test asserts,
    ONLY) call, and would answer PRIVATE on any subsequent call -- exactly
    what a DNS-rebinding attacker's authoritative server does (public for
    the check, private once it believes the victim is now connecting). The
    fetch must succeed (using the pinned IP from that one lookup) and
    getaddrinfo must be called EXACTLY ONCE: a second call would mean this
    module re-resolved at "connect time", which is precisely the gap the
    attacker needs."""
    call_count = {"n": 0}

    def _rebinding_dns(host, port):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [(None, None, None, None, ("93.184.216.34", 0))]  # public
        return [(None, None, None, None, ("127.0.0.1", 0))]  # would poison a 2nd lookup

    monkeypatch.setattr(net.socket, "getaddrinfo", _rebinding_dns)

    resp = _FakeResponse(status_code=200, headers={"Content-Type": "text/html"}, chunks=[b"ok"])
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    result = safe_fetch_text("https://evil.example.com/")

    assert result == ("ok", b"ok", "text/html", "utf-8")  # succeeded -- not wrongly blocked
    assert call_count["n"] == 1  # exactly one resolution: no re-resolve-at-connect gap


# ---------------------------------------------------------------------------
# _host_header -- the explicit Host header every pinned request carries
# (the actual TCP connection targets a bare IP, so nothing else would tell
# the origin server which virtual host this request is for).
# ---------------------------------------------------------------------------

def test_host_header_omits_default_port():
    assert net._host_header("https://example.com/path") == "example.com"
    assert net._host_header("http://example.com/path") == "example.com"


def test_host_header_includes_non_default_port():
    assert net._host_header("https://example.com:8443/path") == "example.com:8443"


def test_host_header_empty_for_missing_hostname():
    assert net._host_header("http:///no-host") == ""


# ---------------------------------------------------------------------------
# _PinnedHostAdapter -- the mechanism itself. Constructing a urllib3
# connection object via `get_connection_with_tls_context` does NOT open a
# real socket (that only happens lazily on `.urlopen()`), so this is safe,
# real (not mocked) object-level verification with no network at all.
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, url):
        self.url = url


def test_pinned_host_adapter_pins_https_connection_and_keeps_sni_on_real_host():
    adapter = net._PinnedHostAdapter(pinned_ip="93.184.216.34", original_host="example.com")
    conn = adapter.get_connection_with_tls_context(_FakeRequest("https://example.com/path"), verify=True)
    try:
        assert conn.host == "93.184.216.34"  # the actual TCP connect target
        # server_hostname drives BOTH the TLS SNI extension and the
        # certificate hostname match performed during the handshake (see
        # urllib3's HTTPSConnection.connect) -- pinning the socket to the IP
        # must not also repoint these at the IP, or the real certificate
        # would never validate.
        assert conn.conn_kw.get("server_hostname") == "example.com"
    finally:
        conn.close()


def test_pinned_host_adapter_plain_http_has_no_server_hostname_kwarg():
    """Plain `HTTPConnection` (used for http://) has no `server_hostname`
    constructor parameter at all -- passing one would raise. Confirms the
    adapter only adds it for https."""
    adapter = net._PinnedHostAdapter(pinned_ip="93.184.216.34", original_host="example.com")
    conn = adapter.get_connection_with_tls_context(_FakeRequest("http://example.com/path"), verify=True)
    try:
        assert conn.host == "93.184.216.34"
        assert "server_hostname" not in conn.conn_kw
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fake requests.Response for safe_fetch_text / _safe_download / fetch_robots_txt
# tests -- no real network call is ever made.
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, headers=None, chunks=None, encoding="utf-8"):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks if chunks is not None else [b""]
        self.encoding = encoding

    def iter_content(self, chunk_size=65536):
        yield from self._chunks


def _mock_public_dns(monkeypatch, ip="93.184.216.34"):
    """Every fetch test below needs SOME public resolution to get past
    `_resolve_pinned_ip` -- this stands in for a real, benign DNS answer."""
    monkeypatch.setattr(net.socket, "getaddrinfo", lambda host, port: [(None, None, None, None, (ip, 0))])


# ---------------------------------------------------------------------------
# safe_fetch_text
# ---------------------------------------------------------------------------

def test_safe_fetch_text_returns_ok_tuple_with_raw_bytes_content_type_and_charset(monkeypatch):
    """FIX A (Sol review round 2): `safe_fetch_text` no longer decodes -- it
    hands back the UNMODIFIED response bytes plus the charset it detected,
    so the caller (not this module) owns hashing/storing the true raw bytes
    and decoding them separately for canonicalization."""
    _mock_public_dns(monkeypatch)
    html_bytes = b"<html><body>hello</body></html>"
    resp = _FakeResponse(
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
        chunks=[html_bytes],
        encoding="utf-8",
    )
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    result = safe_fetch_text("https://example.com/page")

    assert result == ("ok", html_bytes, "text/html; charset=utf-8", "utf-8")


def test_safe_fetch_text_sends_honest_user_agent(monkeypatch):
    _mock_public_dns(monkeypatch)
    captured = {}

    def _fake_get(self, url, timeout, stream, allow_redirects, headers=None):
        captured["headers"] = headers
        return _FakeResponse(status_code=200, headers={"Content-Type": "text/html"}, chunks=[b"hi"])

    monkeypatch.setattr(requests.Session, "get", _fake_get)
    safe_fetch_text("https://example.com/page")

    assert captured["headers"]["User-Agent"] == USER_AGENT


def test_safe_fetch_text_sets_explicit_host_header_for_pinned_connection(monkeypatch):
    """The actual TCP connection is pinned to a bare IP (via the mounted
    adapter) -- without an explicit `Host` header, the underlying
    http.client/urllib3 connection would otherwise derive one from the IP
    it's connected to, not the real hostname. The URL requests.Session.get
    is called with stays hostname-based; only the connection itself (not
    exercised by this monkeypatch) targets the pinned IP."""
    _mock_public_dns(monkeypatch)
    captured = {}

    def _fake_get(self, url, timeout, stream, allow_redirects, headers=None):
        captured["headers"] = headers
        captured["url"] = url
        return _FakeResponse(status_code=200, headers={"Content-Type": "text/html"}, chunks=[b"hi"])

    monkeypatch.setattr(requests.Session, "get", _fake_get)
    safe_fetch_text("https://example.com/page")

    assert captured["url"] == "https://example.com/page"
    assert captured["headers"]["Host"] == "example.com"


def test_safe_fetch_text_rejects_private_host_without_any_request(monkeypatch):
    monkeypatch.setattr(
        net.socket, "getaddrinfo",
        lambda host, port: [(None, None, None, None, ("169.254.169.254", 0))],
    )

    def _boom(self, *a, **k):
        raise AssertionError("Session.get must not be called for an invalid host")

    monkeypatch.setattr(requests.Session, "get", _boom)
    assert safe_fetch_text("http://169.254.169.254/latest/meta-data/") is None


def test_safe_fetch_text_rejects_non_text_content_type(monkeypatch):
    _mock_public_dns(monkeypatch)
    resp = _FakeResponse(status_code=200, headers={"Content-Type": "application/json"}, chunks=[b"{}"])
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    assert safe_fetch_text("https://example.com/api") is None


def test_safe_fetch_text_accepts_xhtml_content_type(monkeypatch):
    _mock_public_dns(monkeypatch)
    resp = _FakeResponse(
        status_code=200, headers={"Content-Type": "application/xhtml+xml"}, chunks=[b"<html/>"],
    )
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    result = safe_fetch_text("https://example.com/page.xhtml")
    assert result is not None


def test_safe_fetch_text_rejects_non_200_status(monkeypatch):
    _mock_public_dns(monkeypatch)
    resp = _FakeResponse(status_code=404, headers={"Content-Type": "text/html"}, chunks=[b"nope"])
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    assert safe_fetch_text("https://example.com/missing") is None


def test_safe_fetch_text_rejects_over_cap_via_content_length_header(monkeypatch):
    _mock_public_dns(monkeypatch)
    resp = _FakeResponse(
        status_code=200,
        headers={"Content-Type": "text/html", "Content-Length": "999999999"},
        chunks=[b"whatever"],
    )
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    assert safe_fetch_text("https://example.com/huge", cap_bytes=10) is None


def test_safe_fetch_text_rejects_over_cap_while_streaming_even_without_content_length(monkeypatch):
    """A server that lies about (or omits) Content-Length must still be
    capped DURING streaming, not just via the header."""
    _mock_public_dns(monkeypatch)
    big_chunks = [b"x" * 10 for _ in range(10)]  # 100 bytes total, no Content-Length
    resp = _FakeResponse(status_code=200, headers={"Content-Type": "text/html"}, chunks=big_chunks)
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    assert safe_fetch_text("https://example.com/huge", cap_bytes=50) is None


def test_safe_fetch_text_handles_request_exception(monkeypatch):
    _mock_public_dns(monkeypatch)

    def _boom(self, *a, **k):
        raise requests.RequestException("connection reset")

    monkeypatch.setattr(requests.Session, "get", _boom)
    assert safe_fetch_text("https://example.com/flaky") is None


def test_safe_fetch_text_returns_redirect_tuple_on_3xx_with_location(monkeypatch):
    """FIX B (Sol review round 2): `safe_fetch_text` no longer follows a
    redirect internally -- `allow_redirects=False` is set on the single GET
    it performs, and a 3xx with a `Location` header comes back as a
    `("redirect", absolute_location)` tuple. The redirect TARGET is never
    fetched by this function at all -- that decision belongs to the caller
    (`worker.connectors.web_fetch.capture_pages`), which alone has the
    robots.txt/rate-limit policy context to decide whether to follow it."""
    _mock_public_dns(monkeypatch)
    get_calls = []

    redirect_resp = _FakeResponse(status_code=302, headers={"Location": "https://example.com/final"})

    def _fake_get(self, url, timeout, stream, allow_redirects, headers=None):
        get_calls.append(url)
        assert allow_redirects is False
        return redirect_resp

    monkeypatch.setattr(requests.Session, "get", _fake_get)

    result = safe_fetch_text("https://example.com/start")
    assert result == ("redirect", "https://example.com/final")
    assert get_calls == ["https://example.com/start"]  # exactly one GET -- no internal follow


def test_safe_fetch_text_redirect_resolves_relative_locations_via_urljoin(monkeypatch):
    """FIX B: `Location` is frequently relative -- an absolute path (`/x`), a
    dot-relative path (`../y`), or a scheme-relative reference (`//host/z`)
    -- and using any of those raw (instead of resolving them against the
    request URL via `urljoin`) would silently break the redirect, or --
    worse -- resolve to something unintended."""
    _mock_public_dns(monkeypatch)
    cases = [
        ("https://example.com/a/b/c", "/x", "https://example.com/x"),
        ("https://example.com/a/b/c", "../y", "https://example.com/a/y"),
        ("https://example.com/a/b/c", "//other.example.com/z", "https://other.example.com/z"),
    ]
    for request_url, location, expected in cases:
        resp = _FakeResponse(status_code=302, headers={"Location": location})
        monkeypatch.setattr(
            requests.Session, "get",
            lambda self, url, timeout, stream, allow_redirects, headers=None, _resp=resp: _resp,
        )
        assert safe_fetch_text(request_url) == ("redirect", expected)


def test_safe_fetch_text_redirect_without_location_header_returns_none(monkeypatch):
    _mock_public_dns(monkeypatch)
    resp = _FakeResponse(status_code=302, headers={})
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )
    assert safe_fetch_text("https://example.com/start") is None


def test_safe_fetch_text_does_not_validate_or_fetch_the_redirect_target_itself(monkeypatch):
    """The SSRF host-validation this module performs still runs on exactly
    the URL being fetched -- a redirect TARGET (even an attacker-controlled
    one pointing at a private/cloud-metadata address) is not resolved or
    fetched by this call at all; validating and fetching it (if the caller
    decides to follow it) happens on the CALLER'S next, separate call to
    this same function, which then re-runs this exact discipline against
    that URL."""
    _mock_public_dns(monkeypatch, ip="93.184.216.34")  # only the source host resolves here

    redirect_resp = _FakeResponse(status_code=302, headers={"Location": "http://169.254.169.254/secret"})
    get_calls = []

    def _fake_get(self, url, timeout, stream, allow_redirects, headers=None):
        get_calls.append(url)
        return redirect_resp

    monkeypatch.setattr(requests.Session, "get", _fake_get)

    result = safe_fetch_text("https://example.com/start")
    assert result == ("redirect", "http://169.254.169.254/secret")
    assert get_calls == ["https://example.com/start"]  # target never fetched by THIS call


def test_safe_fetch_text_passes_through_bogus_charset_without_decoding(monkeypatch):
    """FIX A (Sol review round 2): `safe_fetch_text` no longer decodes the
    body at all -- even a bogus/unrecognised encoding name is just handed
    back as the `charset` element of the `ok` tuple, verbatim, rather than
    raising or being silently substituted. Falling back to utf-8/replace on
    an unrecognised codec name is now the CALLER's job (see
    `worker.connectors.web_fetch._decode_raw`)."""
    _mock_public_dns(monkeypatch)
    resp = _FakeResponse(
        status_code=200,
        headers={"Content-Type": "text/html"},
        chunks=[b"hello"],
        encoding="not-a-real-encoding",
    )
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    result = safe_fetch_text("https://example.com/weird-encoding")
    assert result == ("ok", b"hello", "text/html", "not-a-real-encoding")


# ---------------------------------------------------------------------------
# _safe_download -- unchanged behavior sanity check post-move (image-only,
# no custom UA) plus the same pinned-connection discipline as safe_fetch_text.
# ---------------------------------------------------------------------------

def test_safe_download_still_rejects_private_host(monkeypatch):
    monkeypatch.setattr(
        net.socket, "getaddrinfo",
        lambda host, port: [(None, None, None, None, ("127.0.0.1", 0))],
    )
    assert _safe_download("http://127.0.0.1/image.png") is None


def test_safe_download_accepts_image_content_type_and_sends_no_custom_user_agent(monkeypatch):
    _mock_public_dns(monkeypatch)
    captured = {}

    def _fake_get(self, url, timeout, stream, allow_redirects, headers=None):
        captured["headers"] = headers
        return _FakeResponse(status_code=200, headers={"Content-Type": "image/png"}, chunks=[b"\x89PNG"])

    monkeypatch.setattr(requests.Session, "get", _fake_get)
    result = _safe_download("https://example.com/logo.png")

    assert result == (b"\x89PNG", "image/png")
    # _safe_download's own pre-existing behavior: no custom User-Agent
    # (unlike safe_fetch_text's honest UA). An explicit Host header is now
    # always present on BOTH fetch paths (needed for the pinned connection,
    # see FIX 1) -- only the absence of a custom UA is this function's own
    # distinguishing behavior.
    assert "User-Agent" not in captured["headers"]


def test_safe_download_rejects_non_image_content_type(monkeypatch):
    _mock_public_dns(monkeypatch)
    resp = _FakeResponse(status_code=200, headers={"Content-Type": "text/html"}, chunks=[b"<html>"])
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    assert _safe_download("https://example.com/not-an-image") is None


# ---------------------------------------------------------------------------
# fetch_robots_txt -- the FIX 6 support helper: distinguishes "absent"
# (404/410 -> allow-all is the caller's call, not this function's) from
# "ok" (200, parse it) from "error" (anything else -- conservative
# disallow is the caller's call).
# ---------------------------------------------------------------------------

def test_fetch_robots_txt_ok_on_200(monkeypatch):
    _mock_public_dns(monkeypatch)
    body = b"User-agent: *\nDisallow: /private/\n"
    resp = _FakeResponse(status_code=200, headers={"Content-Type": "text/plain"}, chunks=[body])
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    status, text = fetch_robots_txt("https://example.com/robots.txt")
    assert status == "ok"
    assert "Disallow: /private/" in text


def test_fetch_robots_txt_absent_on_404(monkeypatch):
    _mock_public_dns(monkeypatch)
    resp = _FakeResponse(status_code=404, headers={}, chunks=[b"not found"])
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    assert fetch_robots_txt("https://example.com/robots.txt") == ("absent", None)


def test_fetch_robots_txt_absent_on_410(monkeypatch):
    _mock_public_dns(monkeypatch)
    resp = _FakeResponse(status_code=410, headers={}, chunks=[b"gone"])
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    assert fetch_robots_txt("https://example.com/robots.txt") == ("absent", None)


def test_fetch_robots_txt_error_on_server_error(monkeypatch):
    _mock_public_dns(monkeypatch)
    resp = _FakeResponse(status_code=503, headers={}, chunks=[b"unavailable"])
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    assert fetch_robots_txt("https://example.com/robots.txt") == ("error", None)


def test_fetch_robots_txt_error_on_other_client_error(monkeypatch):
    """A non-404/410 4xx (e.g. 403) is also treated conservatively as
    "error" (disallow), not "absent" (allow-all) -- only the two explicit
    "nothing published here" codes get the permissive read."""
    _mock_public_dns(monkeypatch)
    resp = _FakeResponse(status_code=403, headers={}, chunks=[b"forbidden"])
    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: resp,
    )

    assert fetch_robots_txt("https://example.com/robots.txt") == ("error", None)


def test_fetch_robots_txt_error_on_network_exception(monkeypatch):
    _mock_public_dns(monkeypatch)

    def _boom(self, *a, **k):
        raise requests.RequestException("timed out")

    monkeypatch.setattr(requests.Session, "get", _boom)
    assert fetch_robots_txt("https://example.com/robots.txt") == ("error", None)


def test_fetch_robots_txt_error_on_invalid_host(monkeypatch):
    monkeypatch.setattr(
        net.socket, "getaddrinfo",
        lambda host, port: [(None, None, None, None, ("127.0.0.1", 0))],
    )
    assert fetch_robots_txt("http://127.0.0.1/robots.txt") == ("error", None)


def test_fetch_robots_txt_error_on_midbody_read_timeout(monkeypatch):
    """FIX C (Sol review round 2): a read error (timeout, connection reset,
    chunked-encoding violation, ...) raised WHILE ITERATING the streamed
    body -- not just at connect/headers time -- must map to the same
    conservative ('error', None) outcome, not escape as an unhandled
    `requests.RequestException` and crash the caller. `_get_robots` caches
    whatever this function returns per domain, so this becomes a cached
    'unverifiable, disallow' decision for the rest of the run, not a
    repeated fetch attempt."""
    _mock_public_dns(monkeypatch)

    class _MidBodyTimeoutResponse:
        status_code = 200
        headers = {}

        def iter_content(self, chunk_size=65536):
            yield b"User-agent: *\n"
            raise requests.exceptions.ReadTimeout("timed out mid-body")

    monkeypatch.setattr(
        requests.Session, "get",
        lambda self, url, timeout, stream, allow_redirects, headers=None: _MidBodyTimeoutResponse(),
    )

    assert fetch_robots_txt("https://example.com/robots.txt") == ("error", None)


# ---------------------------------------------------------------------------
# canonical_host -- FIX D (Sol review round 2): the per-host key the robots
# cache and DomainRateLimiter must key on, instead of the raw
# `urlparse(url).netloc` (which carries userinfo and a port even when it's
# the scheme's own default, and preserves case) -- so `user@host`, `host`,
# and `host:443` no longer split one domain's rate-limit/robots budget
# across multiple cache entries.
# ---------------------------------------------------------------------------

def test_canonical_host_lowercases_and_strips_default_port():
    assert canonical_host("https://EXAMPLE.com:443/path") == "example.com"
    assert canonical_host("http://EXAMPLE.com:80/path") == "example.com"


def test_canonical_host_keeps_non_default_port():
    assert canonical_host("https://example.com:8443/path") == "example.com:8443"


def test_canonical_host_drops_userinfo():
    assert canonical_host("https://user:pass@example.com/path") == "example.com"


def test_canonical_host_empty_for_missing_hostname():
    assert canonical_host("http:///no-host") == ""


def test_canonical_host_merges_userinfo_default_port_and_case_variants():
    """FIX D's actual motivating bug: `user@host`, bare `host`, `host:443`,
    a userinfo+port combination, and an upper-case spelling must all key to
    the SAME canonical value -- otherwise one domain's rate-limit/robots
    budget silently splits across multiple cache/limiter keys."""
    variants = [
        "https://example.com/a",
        "https://example.com:443/b",
        "https://user@example.com/c",
        "https://user:pass@example.com:443/d",
        "https://EXAMPLE.COM/e",
    ]
    hosts = {canonical_host(u) for u in variants}
    assert hosts == {"example.com"}
