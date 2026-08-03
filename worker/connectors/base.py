"""Shared fetch discipline for site-capture connectors: robots.txt
compliance and per-domain rate limiting. Deliberately connector-agnostic --
`worker.connectors.web_fetch` is Phase 2's only connector, but a future
connector reuses this exact discipline rather than reimplementing it.

Static-HTML only: nothing here (or in `worker.net.safe_fetch_text`) executes
JavaScript or renders a page -- a connector built on this module fetches and
canonicalizes markup as received over the wire, never a browser-rendered
DOM.
"""
import logging
import time
import urllib.robotparser

from worker.net import USER_AGENT, canonical_host, canonical_origin, fetch_robots_txt

logger = logging.getLogger(__name__)

# Fed to RobotFileParser.parse() to force a full disallow for every
# user-agent -- used when a domain's robots.txt could not be verified (see
# `_get_robots`'s "error" branch) instead of the "no rules found -> allow
# everything" behavior `RobotFileParser` itself defaults to when nothing at
# all is parsed.
_DISALLOW_ALL_ROBOTS_LINES = ["User-agent: *", "Disallow: /"]


def _domain_of(url: str) -> str:
    """The per-domain key used for the robots.txt cache and the
    `DomainRateLimiter`. Delegates to `worker.net.canonical_host` (FIX D,
    Sol review round 2) rather than `urlparse(url).netloc.lower()` (this
    function's own pre-fix body): the raw netloc carries userinfo and an
    explicit-even-when-default port, so `user@host`, `host`, and `host:443`
    used to key to three DIFFERENT cache/limiter entries for what is really
    ONE domain's budget. `canonical_host` is also what
    `worker.connectors.web_fetch.capture_pages` now keys its own page
    fetches (and a redirect target's fetch) on, so a domain named either way
    always lands on the exact same key here.
    """
    return canonical_host(url)


def _robots_txt_url(url: str) -> str:
    # Per-origin (scheme + canonical host + non-default port), so http:// and
    # https:// each fetch their own robots.txt (Sol review round 3).
    return f"{canonical_origin(url)}/robots.txt"


def _get_robots(url: str, cache: dict, limiter: "DomainRateLimiter") -> urllib.robotparser.RobotFileParser:
    """Fetch (once per domain, THROUGH the same SSRF-safe, DNS-pinned
    discipline every page fetch uses -- `worker.net.fetch_robots_txt`) and
    parse robots.txt for `url`'s domain, caching the parsed result in
    `cache` (keyed by domain) so a many-page plan for one domain fetches
    robots.txt exactly once.

    `limiter` is the SAME per-domain `DomainRateLimiter` instance the caller
    uses for page fetches (FIX 5): `wait(domain)` runs before the request,
    and `record(domain, counts_as_page=False)` runs in a `finally` after it,
    so (a) a domain's robots.txt lookup and its first page fetch can never
    land back-to-back inside `min_interval_seconds`, and (b) an exception
    mid-fetch still consumes that timing budget exactly like a page fetch's
    own exception would. `counts_as_page=False` because a robots.txt lookup
    is not one of the pages `max_pages_per_domain_per_run` is meant to
    bound -- it happens exactly once per domain per run regardless of how
    many pages that domain has in the plan; folding it into the same
    counter `allow()` checks would silently shrink every domain's effective
    per-run page budget by one.

    Three distinct outcomes from `fetch_robots_txt` (FIX 6 -- see that
    function's own docstring for the full status contract):
      - `"absent"` (the robots.txt request itself returned 404/410): no
        rules published for this domain -- `RobotFileParser.parse([])`'s
        own documented behavior is to allow everything when it has parsed
        no rules, which is the correct read here.
      - `"ok"`: parse and honor the fetched rules.
      - `"error"` (anything else -- any other non-2xx status, a DNS/SSRF
        host-validation failure, a timeout or connection error, or an
        unresolved redirect): this domain's robots policy could NOT be
        verified, so the conservative, safe choice is a full DISALLOW for
        the rest of this run -- the opposite of the pre-fix behavior, which
        collapsed every failure mode (including "the robots.txt server just
        started 500ing") into "couldn't fetch it, so allow everything".
    """
    # The robots cache keys on the ORIGIN (scheme+host+port) so http:// and
    # https:// get separate policies (Sol review round 3); the rate LIMITER keys
    # on the host (`domain`) so both schemes share one politeness clock.
    domain = _domain_of(url)
    origin = canonical_origin(url)
    if origin in cache:
        return cache[origin]

    rp = urllib.robotparser.RobotFileParser()
    limiter.wait(domain)
    try:
        status, body = fetch_robots_txt(_robots_txt_url(url))
    finally:
        limiter.record(domain, counts_as_page=False)

    if status == "absent":
        rp.parse([])
    elif status == "ok":
        rp.parse((body or "").splitlines())
    else:
        logger.warning(
            "web_fetch: robots.txt for %s could not be verified (status=%s) -- "
            "disallowing this domain for the rest of the run rather than "
            "assuming it's open",
            origin, status,
        )
        rp.parse(_DISALLOW_ALL_ROBOTS_LINES)
    cache[origin] = rp
    return rp


def robots_allowed(url: str, cache: dict, limiter: "DomainRateLimiter") -> bool:
    """True iff `USER_AGENT` may fetch `url` per its domain's robots.txt
    (cached per-domain in `cache`, which callers should create ONCE per job
    run and pass to every call). `limiter` is the SAME per-domain
    `DomainRateLimiter` instance the caller uses for its own page fetches --
    see `_get_robots` for why the robots.txt lookup itself must be routed
    through it too. A robots-denied URL must be SKIPPED by the caller --
    never fetched -- and the skip recorded (see
    `worker.connectors.web_fetch.capture_pages`'s `robots_skipped` counter
    and per-page log line); this function only answers the yes/no question,
    it does not itself record anything.
    """
    rp = _get_robots(url, cache, limiter)
    return rp.can_fetch(USER_AGENT, url)


class DomainRateLimiter:
    """Enforces, per domain, both halves of a connector's `rate_limits`
    contract (`research_connectors.rate_limits`, e.g.
    `{"min_interval_seconds": 3, "max_pages_per_domain_per_run": 40}` for
    web.fetch): a minimum wait between requests to the SAME domain, and a
    hard cap on how many pages from that domain this one run may fetch.
    `rate_limits` is always PASSED IN by the caller (read from the
    connector's own row, or from the job's params) -- never hardcoded here,
    so a rate_limits change takes effect without a code change.

    Every outbound request to a domain -- a page fetch OR a robots.txt
    lookup -- must go through `wait()` before it and `record()` (in a
    `finally`) after it, so the SAME timing clock governs both (FIX 5): a
    domain's robots.txt lookup and its first page fetch can never land
    back-to-back inside `min_interval_seconds`, and an exception mid-fetch
    still consumes that timing budget rather than leaving the clock
    unadvanced.

    `sleep_fn`/`time_fn` are injectable so a test can drive `wait()`
    deterministically with no real `time.sleep` and no real clock.
    """

    def __init__(self, rate_limits: dict, sleep_fn=time.sleep, time_fn=time.monotonic):
        rate_limits = rate_limits or {}
        self._min_interval = float(rate_limits.get("min_interval_seconds") or 0)
        max_pages = rate_limits.get("max_pages_per_domain_per_run")
        self._max_pages = int(max_pages) if max_pages is not None else None
        self._sleep_fn = sleep_fn
        self._time_fn = time_fn
        self._last_request_at: dict = {}
        self._pages_this_run: dict = {}

    def allow(self, domain: str) -> bool:
        """True iff another page may be fetched from `domain` in this run --
        i.e. `max_pages_per_domain_per_run` has not yet been reached. MUST
        be checked by the caller BEFORE fetching; this class never fetches
        anything itself and cannot enforce the cap on its own."""
        if self._max_pages is None:
            return True
        return self._pages_this_run.get(domain, 0) < self._max_pages

    def wait(self, domain: str) -> None:
        """Block (via `sleep_fn`) until at least `min_interval_seconds` have
        elapsed since the last recorded request to `domain` -- ANY request,
        page or robots.txt (see class docstring). A no-op for this domain's
        first request in the run (nothing recorded yet) or when
        `min_interval_seconds` is 0/unset."""
        if self._min_interval <= 0:
            return
        last = self._last_request_at.get(domain)
        if last is None:
            return
        elapsed = self._time_fn() - last
        remaining = self._min_interval - elapsed
        if remaining > 0:
            self._sleep_fn(remaining)

    def record(self, domain: str, counts_as_page: bool = True) -> None:
        """Mark that a request to `domain` just happened. Call AFTER the
        fetch (successful or not -- an attempted request still occupies the
        rate-limit window and, for a real page, counts toward the per-domain
        page cap), IN A `finally` around the fetch, so an exception mid-fetch
        still advances the clock -- the next `wait()`/`allow()` check for
        this domain must stay accurate even when the request itself blew up.

        Always updates the `min_interval_seconds` timing clock. `counts_as_page`
        (default True, unchanged for every page-fetch call site) additionally
        bumps the `max_pages_per_domain_per_run` counter `allow()` checks --
        pass `counts_as_page=False` for a request that consumes rate-limit
        TIMING budget but is not itself one of the pages the per-run page cap
        is meant to bound (the connector's own robots.txt lookup, made once
        per domain per run regardless of how many pages that domain has in
        the plan -- see `_get_robots`).
        """
        self._last_request_at[domain] = self._time_fn()
        if counts_as_page:
            self._pages_this_run[domain] = self._pages_this_run.get(domain, 0) + 1
