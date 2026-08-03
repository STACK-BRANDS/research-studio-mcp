"""Shared SSRF-safe network boundary for the Research Studio worker.

Extracted from `worker.ingest` (Phase 2 Task: web.fetch connector) so the
boundary is owned by its own module rather than living inside an ads-domain
one. `worker.ingest` imports `_is_public_ip`/`_validate_url_host`/
`_safe_download`/`MAX_IMAGE_BYTES`/`ALLOWED_SCHEMES` back from here --
straight re-exports, no behavior change -- so its own `fetch_images` path
(creative image downloads) is completely unaffected by this move.

Every fetcher in this worker (ad-creative images today, the web.fetch
site-capture connector from Phase 2 on) goes through the SAME discipline:

  - scheme allowlist (http/https only)
  - the URL's hostname is DNS-resolved EXACTLY ONCE and EVERY resolved
    address must be a public IP (no private/loopback/link-local/reserved/
    multicast/unspecified address) -- this is the actual SSRF check: a
    hostname that LOOKS public but resolves to 127.0.0.1 (DNS rebinding) or
    169.254.x.x (cloud metadata) is rejected exactly like a literal
    private-IP URL
  - the connection is then PINNED to that exact validated IP -- `requests`/
    urllib3 is never given the hostname to resolve again, closing the
    classic DNS-rebinding TOCTOU where a resolve-then-connect gap lets an
    attacker's DNS server answer "public" for the validation lookup and
    "private" for the connection itself (see `_resolve_pinned_ip` and
    `_PinnedHostAdapter` below)
  - at most ONE redirect hop, and that hop's `Location` goes through the
    exact same resolve-once-then-pin discipline before it is ever fetched --
    an attacker cannot use a public-looking redirect source to bounce us at
    an internal address, NOR bounce us via a rebind on the redirect target
    (`safe_fetch_text` is the one exception -- see below: it hands the
    redirect back to its caller instead of following it itself, but the
    target still gets this exact discipline, just via a separate call)
  - a byte cap enforced BOTH via a declared Content-Length (fail fast, no
    bytes read) AND while actually streaming the body (a server that lies
    about Content-Length, or omits it, cannot force us to buffer past the
    cap)
  - a short, fixed timeout

`_safe_download` (images) and `fetch_robots_txt` share this exact boundary
(via `_fetch_with_one_redirect`, which follows its one redirect hop
internally) and differ only in which Content-Type prefix they accept, what
headers they send, and what they hand back.

`safe_fetch_text` (text/html -- the web.fetch connector's page fetch) shares
the SAME resolve-once-then-pin discipline for whichever single URL it is
asked to fetch, but -- unlike the other two -- does NOT follow a redirect
internally (FIX B, Sol review round 2): it sets `allow_redirects=False` and
returns a `("redirect", absolute_location)` tuple instead, leaving the
decision of whether/how to follow that redirect to
`worker.connectors.web_fetch.capture_pages`, the only caller with the
domain/robots-policy context needed to re-check the SAME robots.txt +
rate-limit discipline against the redirect TARGET before ever fetching it.
Fetching that target (if the connector decides to) is just another call to
this same `safe_fetch_text`, so it gets this exact same SSRF-pinning
discipline all over again -- moving the follow-or-not DECISION out of this
module does not weaken the boundary itself.
"""
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = {"http", "https"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_TEXT_CAP_BYTES = 2 * 1024 * 1024
_REQUEST_TIMEOUT_SECONDS = 30

# Honest, identifiable UA for the web.fetch connector (and its robots.txt
# reads, which MUST use the same string `can_fetch()` is asked about --
# a robots.txt's rules are addressed to a user-agent name, and checking
# under one UA while fetching under another would silently ignore rules
# meant for us). `_safe_download` (images, pre-existing) deliberately keeps
# its prior behavior of NOT sending a custom UA -- that is unchanged here.
USER_AGENT = "Mozilla/5.0 (compatible; ResearchFetch/1.0)"


def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_pinned_ip(hostname: str):
    """Resolve `hostname` EXACTLY ONCE and return the single IP every
    subsequent connection to it in this fetch MUST be pinned to -- or None
    if DNS resolution failed, returned nothing, or ANY resolved address is
    private/loopback/link-local/reserved/multicast/unspecified.

    This is the one and only DNS-resolution call site for a fetch (or a
    redirect hop's fetch) in this module. Its return value is not just a
    yes/no validation signal -- it IS the IP the caller connects to
    (`_PinnedHostAdapter` below pins the actual TCP connection to it). That
    is what closes the DNS-rebinding TOCTOU: resolving again later, at
    "connect time", would let an attacker's authoritative DNS server answer
    differently (public here, private then) and connect us somewhere this
    check never actually saw.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return None
    if not infos:
        return None
    ips = [info[4][0] for info in infos]
    if not all(_is_public_ip(ip) for ip in ips):
        return None
    return ips[0]


def _validate_url_host(url: str) -> bool:
    """Bool-only scheme+host check: allowed scheme, present hostname, and
    `_resolve_pinned_ip` succeeds for it. Kept as a standalone yes/no
    predicate for backward compatibility (`worker.ingest`'s re-export, and
    any caller that only needs the answer, not a connection) -- but NOTE
    that `_fetch_with_one_redirect` below does NOT call this; it calls
    `_resolve_pinned_ip` itself and pins to the exact IP that call returns,
    so a fetch never resolves a hostname twice (once to validate, again to
    connect) the way the pre-fix code did.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.hostname:
        return False
    return _resolve_pinned_ip(parsed.hostname) is not None


_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonical_host(url: str) -> str:
    """The canonical per-host key `worker.connectors.base`'s robots.txt
    cache and `DomainRateLimiter` must key on (FIX D, Sol review round 2) --
    instead of the raw `urlparse(url).netloc` those modules keyed on before
    this fix, which carries userinfo (`user:pass@host`) and an EXPLICIT port
    even when it's the scheme's own default (`host:443` for an `https://`
    URL), and preserves whatever case the URL happened to spell the host in.
    Each of those is the SAME domain for rate-limiting/robots purposes, so
    keying on the raw netloc silently splits one domain's
    `min_interval_seconds` clock and `max_pages_per_domain_per_run` budget
    across multiple cache entries: `https://user@example.com/`,
    `https://EXAMPLE.com/`, and `https://example.com:443/` would otherwise
    each get their own independent budget instead of sharing one.

    `urlparse(url).hostname` already lowercases the host and excludes
    userinfo on its own (that part was never the bug) -- this function's own
    job is dropping a port when it is the scheme's default (so `:443`/`:80`
    collapse onto the bare host) while KEEPING a genuinely distinct port
    (`:8443`) that really does identify a different origin. Used
    consistently for every domain this connector touches -- a plan's own
    page URLs AND a redirect target's URL (FIX B) -- so the SAME host is
    never split across two different keys no matter which of those forms
    named it.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    port = parsed.port
    if port is not None and port != _DEFAULT_PORTS.get(parsed.scheme):
        return f"{hostname}:{port}"
    return hostname


def canonical_origin(url: str) -> str:
    """Canonical `scheme://host[:port]` origin — the key robots.txt policy MUST
    be cached and fetched under (Sol review round 3). `canonical_host` alone
    collapses `http://host` and `https://host` to one key, but robots.txt is
    served PER-ORIGIN (http and https can publish different files), so a
    scheme-changing same-host redirect (e.g. https->http) must fetch and honor
    the TARGET origin's own robots.txt, not reuse the source origin's cached
    parser. The `DomainRateLimiter` deliberately stays keyed on `canonical_host`
    (politeness is per host, regardless of scheme); only robots keys on origin.
    """
    scheme = (urlparse(url).scheme or "").lower()
    return f"{scheme}://{canonical_host(url)}"


def _host_header(url: str) -> str:
    """The `Host` header value for `url`'s real hostname (with a non-default
    port appended, matching normal HTTP `Host` header conventions). Passed
    explicitly on every pinned request because the actual TCP connection
    targets a bare IP, not this hostname -- without an explicit `Host`
    header, the underlying `http.client`/urllib3 connection would otherwise
    auto-derive one from the IP it's connected to, breaking any
    virtual-hosted origin (and disagreeing with the TLS SNI name we send,
    which stays on the real hostname -- see `_PinnedHostAdapter`)."""
    parsed = urlparse(url)
    if not parsed.hostname:
        return ""
    return f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname


class _PinnedHostAdapter(HTTPAdapter):
    """A `requests` `HTTPAdapter` that forces the actual TCP connection for
    every request routed through it to `pinned_ip`, while keeping TLS SNI
    and certificate hostname verification pointed at `original_host` (the
    URL's real hostname) for an `https://` request.

    This is the mechanism that turns "we resolved this hostname and
    confirmed it's public" into "we are GUARANTEED to connect to that exact
    IP" -- `requests`/urllib3 is never given the hostname to resolve on its
    own, so it cannot land on a different (attacker-rebound) address between
    our validation and the actual connect.

    Implementation: `HTTPAdapter.get_connection_with_tls_context` (the
    modern, non-deprecated hook -- see requests>=2.32.2) builds the usual
    `(host_params, pool_kwargs)` pair from the request's OWN url (still the
    original hostname-based URL; only the connection this adapter hands back
    is repointed). `host_params["host"]` is overridden to `pinned_ip` --
    this is the value urllib3 actually opens a socket to. For `https`,
    `pool_kwargs["server_hostname"]` is set to `original_host`: urllib3's
    `HTTPSConnection.connect()` uses `server_hostname` (falling back to
    `self.host`, which here is the pinned IP, if not set) for BOTH the TLS
    SNI extension AND the certificate hostname match performed during the
    handshake -- so the certificate presented by the pinned IP is verified
    against the real domain name, exactly as if we'd connected to the
    hostname directly. (`server_hostname` is an `HTTPSConnection`-only
    constructor parameter -- plain `HTTPConnection`, used for `http://`,
    doesn't accept it, so it's only added for the https scheme.)
    """

    def __init__(self, pinned_ip: str, original_host: str):
        self._pinned_ip = pinned_ip
        self._original_host = original_host
        super().__init__()

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(request, verify, cert)
        if host_params.get("scheme") == "https":
            pool_kwargs["server_hostname"] = self._original_host
        host_params["host"] = self._pinned_ip
        return self.poolmanager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)


def _fetch_pinned(url: str, headers: dict, timeout: int = _REQUEST_TIMEOUT_SECONDS):
    """Resolve `url`'s hostname EXACTLY ONCE (`_resolve_pinned_ip`), and --
    only if every resolved address is public -- issue a single GET pinned to
    that exact IP (`_PinnedHostAdapter`), with an explicit `Host` header
    naming the real hostname. `allow_redirects=False`: this function performs
    ONE HTTP request only; following a redirect (with its own fresh
    resolve+pin) is the caller's job (`_fetch_with_one_redirect`).

    Returns the `requests.Response`, or `None` if the scheme/hostname is
    invalid, DNS resolution failed, any resolved address is private, or the
    request itself raised `requests.RequestException` (timeout, connection
    reset, TLS/cert failure against the pinned IP, etc).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.hostname:
        return None

    pinned_ip = _resolve_pinned_ip(parsed.hostname)
    if pinned_ip is None:
        return None

    request_headers = dict(headers or {})
    request_headers.setdefault("Host", _host_header(url))

    session = requests.Session()
    session.mount("http://", _PinnedHostAdapter(pinned_ip, parsed.hostname))
    session.mount("https://", _PinnedHostAdapter(pinned_ip, parsed.hostname))
    try:
        return session.get(
            url, timeout=timeout, stream=True, allow_redirects=False, headers=request_headers,
        )
    except requests.RequestException:
        return None
    finally:
        session.close()


def _fetch_with_one_redirect(url: str, headers: dict, timeout: int = _REQUEST_TIMEOUT_SECONDS):
    """Shared fetch core for `_safe_download` and `fetch_robots_txt`: pinned
    GET of `url`, following AT MOST ONE redirect hop -- and the redirect
    target goes through the EXACT SAME resolve-once-then-pin discipline as
    the original URL (a public-looking redirect source must not be able to
    bounce us at a private address, nor at a rebound one).

    NOT used by `safe_fetch_text` (FIX B, Sol review round 2) -- that
    function's redirect (if any) is followed, or not, by its caller
    (`worker.connectors.web_fetch.capture_pages`), which alone has the
    robots.txt/rate-limit policy context to decide. `_safe_download` (images)
    and `fetch_robots_txt` have no such per-domain policy to re-check, so
    they keep this internal one-hop behavior unchanged.

    Returns the final `requests.Response` (caller still checks its own
    status_code/Content-Type/etc, exactly as before this fix), or `None` if
    host validation fails at either hop, the redirect response has no
    `Location` header, or either GET raised a network error.
    """
    resp = _fetch_pinned(url, headers, timeout)
    if resp is None:
        return None

    if 300 <= resp.status_code < 400:
        location = resp.headers.get("Location")
        if not location:
            return None
        resp = _fetch_pinned(location, headers, timeout)
        if resp is None:
            return None

    return resp


def _safe_download(url: str, cap_bytes: int = MAX_IMAGE_BYTES):
    """Download `url` with an SSRF boundary: scheme allowlist, DNS-resolved
    private/loopback/link-local/reserved/multicast/unspecified IP rejection
    with the connection PINNED to the exact validated IP (no DNS-rebinding
    TOCTOU), at most one manually-validated-and-pinned redirect hop,
    image/* content-type only, and a byte cap enforced both via
    Content-Length and while streaming. Returns (raw_bytes, content_type) or
    None if any check fails.

    Behavior otherwise UNCHANGED from its original home in worker.ingest --
    no headers/UA added here; that stays this function's pre-existing
    behavior. Only the transport (pinned connection vs. a bare
    `requests.get` that could re-resolve the host) changed.
    """
    resp = _fetch_with_one_redirect(url, headers={})
    if resp is None:
        return None

    if resp.status_code != 200:
        return None

    content_type = resp.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        return None

    content_length = resp.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > cap_bytes:
                return None
        except ValueError:
            pass

    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > cap_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks), content_type


def _is_text_content_type(content_type: str) -> bool:
    base = content_type.split(";")[0].strip().lower()
    return base.startswith("text/") or base == "application/xhtml+xml"


def safe_fetch_text(url: str, cap_bytes: int = DEFAULT_TEXT_CAP_BYTES):
    """Fetch `url` as text/html under the SAME SSRF discipline as
    `_safe_download`: scheme allowlist, DNS-resolved public-IP-only host
    validation with the connection PINNED to the exact validated IP (no
    DNS-rebinding TOCTOU), a byte cap enforced via Content-Length AND while
    streaming, and a short timeout. Differs from `_safe_download` in
    accepted Content-Type (text/*, or application/xhtml+xml -- some servers
    serve HTML under that type), in sending the honest, identifiable
    `USER_AGENT` (robots.txt rules must be checked and honored under the
    exact same UA string we fetch with) -- and, as of FIX B (Sol review
    round 2), in how a redirect is handled: this function performs exactly
    ONE GET, with `allow_redirects=False` (via `_fetch_pinned`), and does
    NOT follow a redirect itself. Only the connector
    (`worker.connectors.web_fetch.capture_pages`) has the robots.txt/
    rate-limit policy context to decide whether a redirect target should be
    fetched at all, so that decision -- and the re-check it requires -- is
    entirely the connector's.

    Returns one of three shapes:

      - `("ok", raw_bytes, content_type, charset)` -- a 200 response with an
        accepted Content-Type, within the byte cap. `raw_bytes` is the
        UNMODIFIED response body (FIX A, Sol review round 2): this function
        no longer decodes it -- the caller must hash and store these EXACT
        bytes, not a decoded-then-re-encoded copy (decoding then
        re-encoding, especially with `errors="replace"`, can change the
        bytes -- and thus the hash -- even for a response whose raw bytes
        were unique). `charset` is the encoding `requests` detected from the
        Content-Type header (or its own default), falling back to "utf-8" if
        none was detected; the caller is responsible for decoding with it
        (and for falling back again to utf-8/replace on an unrecognised
        codec name -- this function never decodes, so it can no longer do
        that fallback itself).
      - `("redirect", absolute_location)` -- a 3xx response with a
        `Location` header, resolved to an absolute URL via
        `urljoin(url, location)` (FIX B: `Location` is frequently relative
        -- an absolute path like `/x`, a dot-relative path like `../y`, or a
        scheme-relative reference like `//host/z` -- and using any of those
        raw would silently break the redirect, or resolve to something
        unintended).
      - `None` -- host validation failed, a network error, a 3xx with no
        `Location` header, a non-2xx/non-3xx status, a rejected
        Content-Type, or over the byte cap.

    The SSRF resolve+validate+pin discipline still runs on exactly the URL
    being fetched here -- whichever URL the caller decides to fetch next
    (the redirect target, if it decides to follow it) goes through this same
    function, and thus this same discipline, all over again.
    """
    resp = _fetch_pinned(url, headers={"User-Agent": USER_AGENT})
    if resp is None:
        return None

    if 300 <= resp.status_code < 400:
        location = resp.headers.get("Location")
        if not location:
            return None
        return "redirect", urljoin(url, location)

    if resp.status_code != 200:
        return None

    content_type = resp.headers.get("Content-Type", "")
    if not _is_text_content_type(content_type):
        return None

    content_length = resp.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > cap_bytes:
                return None
        except ValueError:
            pass

    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        total += len(chunk)
        if total > cap_bytes:
            return None
        chunks.append(chunk)
    raw_bytes = b"".join(chunks)

    charset = resp.encoding or "utf-8"
    return "ok", raw_bytes, content_type, charset


def fetch_robots_txt(url: str, cap_bytes: int = DEFAULT_TEXT_CAP_BYTES):
    """Fetch a robots.txt URL under the SAME pinned SSRF-safe discipline as
    `safe_fetch_text`/`_safe_download` (`_fetch_with_one_redirect`), but
    returning a 3-way outcome `worker.connectors.base._get_robots` needs in
    order to treat "no robots.txt published" and "robots.txt unreachable"
    DIFFERENTLY (see that function's own docstring for the FIX 6 reasoning):

      - `("ok", body)` -- a 200 response; `body` is the decoded robots.txt
        text, to be PARSED and HONORED.
      - `("absent", None)` -- a 404 or 410: this domain has published no
        robots.txt at all (or explicitly removed it). Common convention
        reads this as "no restriction published" -- allow everything.
      - `("error", None)` -- anything else: bad scheme/host, DNS resolution
        failure, a private/rebound address, ANY other non-2xx status
        (401/403/429/5xx/...), a network error or timeout, or a redirect
        this module's one-hop discipline couldn't resolve. The caller
        cannot verify this domain's robots policy from this outcome, and
        must treat it as a conservative DISALLOW rather than silently
        allowing everything -- the opposite of what fell out of the pre-fix
        code, which collapsed every failure mode (including "the robots.txt
        server just 500'd") into "couldn't fetch it, so allow everything".

    No Content-Type gate here (unlike `safe_fetch_text`): a robots.txt body
    is accepted regardless of the -- in practice, often misconfigured --
    Content-Type a server serves it under. Only the status code decides the
    outcome.

    FIX C (Sol review round 2): a read error -- a read-timeout, a connection
    reset, a chunked-encoding violation -- raised WHILE ITERATING the
    streamed body (as opposed to at connect/headers time, already covered by
    the `resp is None` branch above) must map to this SAME conservative
    `("error", None)` outcome, not escape as an unhandled
    `requests.RequestException` and crash the caller. `_get_robots` caches
    whatever this function returns per domain, so a read-timeout mid-body
    becomes a cached "unverifiable, disallow" decision for the rest of the
    run -- not a crash, and not a retry storm.
    """
    resp = _fetch_with_one_redirect(url, headers={"User-Agent": USER_AGENT})
    if resp is None:
        return "error", None

    if resp.status_code in (404, 410):
        return "absent", None

    if resp.status_code != 200:
        return "error", None

    content_length = resp.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > cap_bytes:
                return "error", None
        except ValueError:
            pass

    chunks = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > cap_bytes:
                return "error", None
            chunks.append(chunk)
    except requests.RequestException:
        # FIX C: a read error mid-body (read-timeout, connection reset,
        # chunked-encoding violation, ...) is just as "can't verify this
        # domain's robots policy" as a connect-time failure -- same
        # conservative outcome, cached the same way.
        return "error", None
    raw_bytes = b"".join(chunks)

    encoding = resp.encoding or "utf-8"
    try:
        text = raw_bytes.decode(encoding, errors="replace")
    except LookupError:  # unrecognised encoding name -- fall back rather than raise
        text = raw_bytes.decode("utf-8", errors="replace")

    return "ok", text
