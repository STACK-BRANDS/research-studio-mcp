"""Tests for worker/connectors/base.py (robots.txt compliance + per-domain
rate limiting) and worker/connectors/web_fetch.py (capture_pages, the
web.fetch connector's page-plan loop).

No network, no real Supabase, no real sleep/threading: safe_fetch_text,
worker.net.fetch_robots_txt, jobs.assert_lease, and every worker.store.*
call are monkeypatched directly on the module objects that use them (the
same pattern test_run.py uses for run.ingest), and DomainRateLimiter's
sleep_fn/time_fn are injected fakes.
"""
import hashlib

from worker.canonical import canonical_text
from worker.connectors import base, web_fetch


# ---------------------------------------------------------------------------
# base.robots_allowed / _get_robots -- fetch-once-per-domain caching, robots
# Disallow enforcement, and the FIX 6 three-way fetch outcome:
#   "absent" (404/410)   -> allow everything
#   "ok"     (200)       -> parse and honor
#   "error"  (anything else -- 5xx/timeout/other non-2xx/host failure)
#            -> conservative DISALLOW (the opposite of the pre-fix behavior,
#               which collapsed every failure into "allow everything")
# Plus FIX 5: the robots.txt fetch itself goes through the SAME per-domain
# DomainRateLimiter a page fetch uses (wait() before, record() in a
# finally after, counts_as_page=False so it doesn't eat into
# max_pages_per_domain_per_run).
# ---------------------------------------------------------------------------

_ROBOTS_TXT = "User-agent: *\nDisallow: /private/\n"


class _FakeLimiter:
    """Minimal stand-in for base.DomainRateLimiter for tests that only care
    about robots_allowed's OWN behavior (fetch-once caching, the
    absent/ok/error outcomes) -- records wait()/record() calls so a test can
    assert the robots fetch goes through the same limiter discipline a page
    fetch does (FIX 5), without needing the real class's
    min_interval/max_pages machinery."""

    def __init__(self):
        self.waited = []
        self.recorded = []

    def wait(self, domain):
        self.waited.append(domain)

    def record(self, domain, counts_as_page=True):
        self.recorded.append((domain, counts_as_page))


def test_robots_allowed_permits_path_not_disallowed(monkeypatch):
    monkeypatch.setattr(base, "fetch_robots_txt", lambda url: ("ok", _ROBOTS_TXT))
    cache = {}
    assert base.robots_allowed("https://example.com/public/page", cache, _FakeLimiter()) is True


def test_robots_allowed_denies_disallowed_path(monkeypatch):
    monkeypatch.setattr(base, "fetch_robots_txt", lambda url: ("ok", _ROBOTS_TXT))
    cache = {}
    assert base.robots_allowed("https://example.com/private/page", cache, _FakeLimiter()) is False


def test_robots_allowed_fetches_once_per_domain_then_caches(monkeypatch):
    calls = []

    def _fake_fetch(url):
        calls.append(url)
        return ("ok", _ROBOTS_TXT)

    monkeypatch.setattr(base, "fetch_robots_txt", _fake_fetch)
    cache = {}
    limiter = _FakeLimiter()
    base.robots_allowed("https://example.com/public/a", cache, limiter)
    base.robots_allowed("https://example.com/public/b", cache, limiter)
    base.robots_allowed("https://example.com/private/c", cache, limiter)

    assert calls == ["https://example.com/robots.txt"]  # fetched exactly once
    # FIX 5: the limiter is only touched on the ONE actual fetch, not on the
    # two cache-hit calls.
    assert limiter.waited == ["example.com"]
    assert limiter.recorded == [("example.com", False)]  # doesn't count as a page


def test_robots_allowed_fetches_separately_per_domain(monkeypatch):
    calls = []
    monkeypatch.setattr(base, "fetch_robots_txt", lambda url: calls.append(url) or ("ok", _ROBOTS_TXT))
    cache = {}
    limiter = _FakeLimiter()
    base.robots_allowed("https://a.example.com/page", cache, limiter)
    base.robots_allowed("https://b.example.com/page", cache, limiter)

    assert calls == ["https://a.example.com/robots.txt", "https://b.example.com/robots.txt"]


def test_robots_allowed_missing_robots_txt_allows_everything(monkeypatch):
    """FIX 6: "absent" (404/410) -> allow everything -- unchanged, for this
    SPECIFIC outcome, from the pre-fix "couldn't fetch it" -> allow-all
    behavior."""
    monkeypatch.setattr(base, "fetch_robots_txt", lambda url: ("absent", None))
    cache = {}
    assert base.robots_allowed("https://example.com/private/anything", cache, _FakeLimiter()) is True


def test_robots_allowed_fetch_error_disallows_everything(monkeypatch):
    """FIX 6: an UNVERIFIABLE robots.txt ("error" -- 5xx, timeout,
    connection error, any other non-2xx) must be a conservative full
    DISALLOW -- the OPPOSITE of the pre-fix behavior, which collapsed every
    failure mode into "allow everything"."""
    monkeypatch.setattr(base, "fetch_robots_txt", lambda url: ("error", None))
    cache = {}
    limiter = _FakeLimiter()
    assert base.robots_allowed("https://example.com/anything", cache, limiter) is False
    # Still disallowed on a second path under the same (now-cached) domain.
    assert base.robots_allowed("https://example.com/anything/else", cache, limiter) is False


def test_robots_allowed_disallow_on_error_is_cached_per_domain(monkeypatch):
    calls = []
    monkeypatch.setattr(base, "fetch_robots_txt", lambda url: calls.append(url) or ("error", None))
    cache = {}
    limiter = _FakeLimiter()
    base.robots_allowed("https://example.com/a", cache, limiter)
    base.robots_allowed("https://example.com/b", cache, limiter)
    assert calls == ["https://example.com/robots.txt"]  # only ONE fetch attempt; error cached


def test_robots_allowed_records_limiter_even_when_fetch_errors(monkeypatch):
    """FIX 5: record() runs in a finally -- even a robots.txt fetch that
    resolves to 'error' must still consume the rate-limit timing budget."""
    monkeypatch.setattr(base, "fetch_robots_txt", lambda url: ("error", None))
    limiter = _FakeLimiter()
    base.robots_allowed("https://example.com/a", {}, limiter)
    assert limiter.waited == ["example.com"]
    assert limiter.recorded == [("example.com", False)]


# ---------------------------------------------------------------------------
# base.DomainRateLimiter -- min_interval_seconds wait + max_pages_per_domain
# cap, fully deterministic via injected sleep_fn/time_fn (no real sleep).
# Plus FIX 5's `counts_as_page` split: a robots.txt lookup consumes timing
# budget but not page-cap budget.
# ---------------------------------------------------------------------------

class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_rate_limiter_wait_sleeps_for_remaining_interval():
    clock = _FakeClock(100.0)
    sleeps = []
    limiter = base.DomainRateLimiter(
        {"min_interval_seconds": 3}, sleep_fn=sleeps.append, time_fn=clock,
    )
    limiter.record("example.com")
    clock.advance(1.0)
    limiter.wait("example.com")
    assert sleeps == [2.0]


def test_rate_limiter_wait_is_noop_when_interval_already_elapsed():
    clock = _FakeClock(100.0)
    sleeps = []
    limiter = base.DomainRateLimiter(
        {"min_interval_seconds": 3}, sleep_fn=sleeps.append, time_fn=clock,
    )
    limiter.record("example.com")
    clock.advance(5.0)
    limiter.wait("example.com")
    assert sleeps == []


def test_rate_limiter_wait_is_noop_on_first_request_for_a_domain():
    sleeps = []
    limiter = base.DomainRateLimiter(
        {"min_interval_seconds": 3}, sleep_fn=sleeps.append, time_fn=_FakeClock(0.0),
    )
    limiter.wait("example.com")
    assert sleeps == []


def test_rate_limiter_allow_enforces_max_pages_per_domain_cap():
    limiter = base.DomainRateLimiter({"max_pages_per_domain_per_run": 2})
    assert limiter.allow("example.com") is True
    limiter.record("example.com")
    assert limiter.allow("example.com") is True
    limiter.record("example.com")
    assert limiter.allow("example.com") is False


def test_rate_limiter_allow_tracks_domains_independently():
    limiter = base.DomainRateLimiter({"max_pages_per_domain_per_run": 1})
    limiter.record("a.example.com")
    assert limiter.allow("a.example.com") is False
    assert limiter.allow("b.example.com") is True


def test_rate_limiter_allow_unbounded_when_cap_not_set():
    limiter = base.DomainRateLimiter({})
    for _ in range(50):
        limiter.record("example.com")
    assert limiter.allow("example.com") is True


def test_rate_limiter_record_counts_as_page_false_does_not_consume_page_cap():
    """FIX 5: a robots.txt lookup (`counts_as_page=False`) must not eat into
    `max_pages_per_domain_per_run` -- only real page fetches (the default)
    do."""
    limiter = base.DomainRateLimiter({"max_pages_per_domain_per_run": 1})
    limiter.record("example.com", counts_as_page=False)  # e.g. the robots.txt fetch
    assert limiter.allow("example.com") is True  # cap untouched
    limiter.record("example.com")  # a real page fetch
    assert limiter.allow("example.com") is False  # cap now consumed


def test_rate_limiter_record_counts_as_page_false_still_advances_wait_clock():
    """FIX 5: the min_interval TIMING clock is shared regardless of
    counts_as_page -- a robots.txt lookup must still block a page fetch
    that follows it too soon."""
    clock = _FakeClock(100.0)
    sleeps = []
    limiter = base.DomainRateLimiter(
        {"min_interval_seconds": 3}, sleep_fn=sleeps.append, time_fn=clock,
    )
    limiter.record("example.com", counts_as_page=False)  # e.g. a robots.txt fetch
    clock.advance(1.0)
    limiter.wait("example.com")
    assert sleeps == [2.0]


# ---------------------------------------------------------------------------
# Integration: base.robots_allowed's REAL implementation (not mocked away)
# wired into capture_pages, proving FIX 5 + FIX 6 work together end-to-end.
# ---------------------------------------------------------------------------

def _real_domain_rate_limiter_no_sleep(monkeypatch):
    """Same no-real-sleep wrapper `_wire` below uses, exposed standalone for
    the two integration tests that need the REAL base.robots_allowed/
    DomainRateLimiter (rather than `_wire`'s mocked-away robots_allowed)."""
    _real_limiter_cls = base.DomainRateLimiter

    def _no_sleep_limiter(rate_limits, sleep_fn=None, time_fn=None):
        kwargs = {"sleep_fn": lambda seconds: None}
        if time_fn is not None:
            kwargs["time_fn"] = time_fn
        return _real_limiter_cls(rate_limits, **kwargs)

    monkeypatch.setattr(web_fetch.base, "DomainRateLimiter", _no_sleep_limiter)


def test_capture_pages_robots_fetch_error_disallows_whole_domain_for_the_run(monkeypatch):
    """Integration proof of FIX 5 + FIX 6 wired together: a REAL
    base.robots_allowed backed by a robots.txt fetch that resolves to
    'error' (a 5xx / timeout / connection failure) must disallow the ENTIRE
    domain for the rest of the run -- capture_pages never fetches a single
    page from it."""
    store = _FakeStore()
    monkeypatch.setattr(web_fetch, "store", store)
    monkeypatch.setattr(web_fetch.base, "fetch_robots_txt", lambda url: ("error", None))
    monkeypatch.setattr(web_fetch.jobs, "assert_lease", lambda job_id, claimant: True)
    _real_domain_rate_limiter_no_sleep(monkeypatch)

    fetched_urls = []

    def _track_fetch(url, cap_bytes=None):
        fetched_urls.append(url)
        return ("ok", b"<p>x</p>", "text/html", "utf-8")

    monkeypatch.setattr(web_fetch, "safe_fetch_text", _track_fetch)

    plan = [{"url": "https://example.com/a"}, {"url": "https://example.com/b"}]
    result = web_fetch.capture_pages(_job(), "claimant-1", plan)

    assert fetched_urls == []  # never fetched -- robots policy unverifiable, conservative disallow
    assert result["robots_skipped"] == 2
    assert result["captured"] == 0


def test_capture_pages_robots_fetch_absent_allows_the_domain(monkeypatch):
    """Companion to the error-disallow test: a robots.txt fetch that
    resolves to 'absent' (404/410 -- nothing published) must ALLOW the
    domain -- unchanged, for this outcome, from the pre-fix "couldn't fetch
    it -> allow" read."""
    store = _FakeStore()
    monkeypatch.setattr(web_fetch, "store", store)
    monkeypatch.setattr(web_fetch.base, "fetch_robots_txt", lambda url: ("absent", None))
    monkeypatch.setattr(web_fetch.jobs, "assert_lease", lambda job_id, claimant: True)
    monkeypatch.setattr(
        web_fetch, "safe_fetch_text", lambda url, cap_bytes=None: ("ok", b"<p>x</p>", "text/html", "utf-8"),
    )
    _real_domain_rate_limiter_no_sleep(monkeypatch)

    plan = [{"url": "https://example.com/a"}]
    result = web_fetch.capture_pages(_job(), "claimant-1", plan)

    assert result["robots_skipped"] == 0
    assert result["captured"] == 1


# ---------------------------------------------------------------------------
# web_fetch.capture_pages -- the page-plan loop.
# ---------------------------------------------------------------------------

class _FakeStore:
    """Records every call (in order, in `.calls`) so tests can assert
    storage-first ordering and no-clobber behavior without touching Supabase."""

    def __init__(self, existing_shas=None, upload_exists_at=None, raise_on_url=None):
        self.calls = []
        self.uploaded = []
        self.captures = []
        self.collection_runs = []
        self._existing_shas = existing_shas or set()       # {(sha, source_url)}
        self._upload_exists_at = upload_exists_at or set()  # {path}
        self._raise_on_url = raise_on_url or {}             # {url: Exception}

    def capture_exists(self, content_sha256, source_url):
        self.calls.append(("capture_exists", content_sha256, source_url))
        return (content_sha256, source_url) in self._existing_shas

    def upload_capture_object(self, path, data, content_type):
        self.calls.append(("upload", path, content_type))
        self.uploaded.append((path, data, content_type))
        return path not in self._upload_exists_at

    def create_site_capture(self, url, source_url, raw_content, connector,
                             competitor_id=None, confidence=None, captured_html_path=None,
                             raw_html_sha256=None):
        if url in self._raise_on_url:
            raise self._raise_on_url[url]
        self.calls.append(("create_site_capture", url))
        capture = {
            "url": url, "source_url": source_url, "raw_content": raw_content,
            "connector": connector, "competitor_id": competitor_id,
            "confidence": confidence, "captured_html_path": captured_html_path,
            "raw_html_sha256": raw_html_sha256,
        }
        self.captures.append(capture)
        return f"capture-{len(self.captures)}"

    def start_collection_run(self, connector):
        return "run-1"

    def finish_collection_run(self, run_id, status, records=None):
        self.collection_runs.append((run_id, status, records))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _raw_sha(raw_bytes: bytes) -> str:
    """FIX A (Sol review round 2): the storage path is keyed by the sha256
    of the TRUE raw response bytes -- callers pass `bytes`, not a str, to
    match `safe_fetch_text`'s new `("ok", raw_bytes, content_type, charset)`
    contract."""
    return hashlib.sha256(raw_bytes).hexdigest()


def _wire(monkeypatch, fake_store, fetch_map=None, robots_map=None, lease_values=True):
    """Wire capture_pages' collaborators: worker.store -> fake_store,
    jobs.assert_lease -> lease_values (bool, or a list consumed in order --
    note capture_pages now calls assert_lease up to THREE times per page:
    top-of-loop, immediately before the fetch, and immediately before the
    uploads+mint block, per FIX 4), base.robots_allowed -> robots_map (dict
    url->bool, default allow-all), net.safe_fetch_text -> fetch_map (dict
    url -> `("ok", raw_bytes, content_type, charset)` | `("redirect",
    target)` | `None`, default: `None` for anything not listed -- FIX A/B,
    Sol review round 2, changed this tuple's shape from the old
    `(raw_html, content_type)`)."""
    monkeypatch.setattr(web_fetch, "store", fake_store)

    # capture_pages builds its own DomainRateLimiter internally (production
    # code calls base.DomainRateLimiter(rate_limits) with no sleep_fn/time_fn
    # override -- there is no injection seam at that call site), which
    # defaults to the REAL time.sleep. Multiple test pages sharing a domain
    # would otherwise trigger a genuine ~3s sleep under
    # settings.web_fetch_default_rate_limits' min_interval_seconds=3.
    #
    # Patching time.sleep itself does NOT work here: `sleep_fn=time.sleep`
    # is a default argument value, bound ONCE to the real function object
    # at module-import time -- reassigning time.sleep afterward never
    # touches that already-captured reference. So instead this wraps the
    # REAL DomainRateLimiter class and forces sleep_fn to a no-op on every
    # construction, leaving rate_limits/time_fn (and every other behavior)
    # completely real -- only the actual blocking is removed.
    _real_limiter_cls = base.DomainRateLimiter

    def _no_sleep_limiter(rate_limits, sleep_fn=None, time_fn=None):
        kwargs = {"sleep_fn": lambda seconds: None}
        if time_fn is not None:
            kwargs["time_fn"] = time_fn
        return _real_limiter_cls(rate_limits, **kwargs)

    monkeypatch.setattr(web_fetch.base, "DomainRateLimiter", _no_sleep_limiter)

    if isinstance(lease_values, list):
        it = iter(lease_values)
        monkeypatch.setattr(web_fetch.jobs, "assert_lease", lambda job_id, claimant: next(it))
    else:
        monkeypatch.setattr(web_fetch.jobs, "assert_lease", lambda job_id, claimant: lease_values)

    robots_map = robots_map or {}
    # 3-arg signature (url, cache, limiter) since FIX 5 threads the shared
    # limiter into robots_allowed -- this fake ignores it (the REAL
    # base.robots_allowed/DomainRateLimiter interaction is covered by the
    # dedicated integration tests above, and by base.py's own unit tests).
    monkeypatch.setattr(web_fetch.base, "robots_allowed", lambda url, cache, limiter: robots_map.get(url, True))

    fetch_map = fetch_map or {}
    monkeypatch.setattr(web_fetch, "safe_fetch_text", lambda url, cap_bytes=None: fetch_map.get(url))


def _job(**overrides):
    job = {"id": "job-1", "job_kind": "scrape", "competitor_id": "comp-1", "params": {}}
    job.update(overrides)
    return job


def test_capture_pages_storage_first_ordering_and_correct_paths(monkeypatch):
    store = _FakeStore()
    raw_html = "<p>Hello</p>"
    raw_bytes = raw_html.encode("utf-8")
    text = canonical_text(raw_html)
    raw_sha = _raw_sha(raw_bytes)
    _wire(monkeypatch, store, fetch_map={"https://example.com/a": ("ok", raw_bytes, "text/html", "utf-8")})

    plan = [{"url": "https://example.com/a"}]
    result = web_fetch.capture_pages(_job(), "claimant-1", plan)

    assert result == {"captured": 1, "unchanged": 0, "robots_skipped": 0, "errors": []}
    # Both uploads happened BEFORE the mint (storage-first).
    call_names = [c[0] for c in store.calls]
    assert call_names.index("upload") < call_names.index("create_site_capture")
    assert call_names.count("upload") == 2
    html_path, text_path = store.uploaded[0][0], store.uploaded[1][0]
    content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    # The .html is keyed by the RAW-bytes hash (captured_html_path points at
    # this row's own bytes); the .txt by the CANONICAL-text hash (content_sha256)
    # so it is content-addressed by its own hash (Sol review round 3).
    assert html_path == f"captures/{raw_sha}.html"
    assert text_path == f"captures/{content_sha}.txt"
    # FIX A: the stored '.html' object is the TRUE raw bytes safe_fetch_text
    # handed back -- not a decode-then-re-encode copy.
    assert store.uploaded[0] == (html_path, raw_bytes, "text/html")
    assert store.uploaded[1] == (text_path, text.encode("utf-8"), "text/plain")
    # The mint uses the CANONICAL text as raw_content (server computes
    # content_sha256 from it -- dedup/change-detection stays canonical-text
    # based), and points at the RAW-sha-keyed html object's path.
    capture = store.captures[0]
    assert capture["raw_content"] == text
    assert capture["captured_html_path"] == html_path
    assert capture["connector"] == "web.fetch"
    # Provenance (migration 149): raw_html_sha256 is the SAME raw_sha256 already used to
    # key captured_html_path above -- reused, never recomputed -- so the capture row's
    # provenance hash is guaranteed to match the bytes actually stored there.
    assert capture["raw_html_sha256"] == raw_sha
    assert capture["competitor_id"] == "comp-1"


def test_capture_pages_raw_sha_keying_avoids_cross_page_content_pointer(monkeypatch):
    """FIX 3's actual motivating bug: two DIFFERENT raw pages that
    canonicalize to the SAME text (thus the SAME content_sha256, the dedup/
    change-detection key) must each get their OWN storage object, keyed by
    the hash of their OWN raw bytes -- so a later capture's
    captured_html_path can never point at an earlier, genuinely different
    page's raw bytes."""
    raw_a = "<p>Same   text</p>"    # canonicalizes to "Same text"
    raw_b = "<p>Same    text</p>"   # extra space -- different raw bytes, SAME canonical text
    raw_bytes_a, raw_bytes_b = raw_a.encode("utf-8"), raw_b.encode("utf-8")
    text = canonical_text(raw_a)
    assert canonical_text(raw_b) == text  # sanity: genuinely the same canonical text
    assert raw_a != raw_b                 # sanity: genuinely different raw bytes

    sha_a, sha_b = _raw_sha(raw_bytes_a), _raw_sha(raw_bytes_b)
    assert sha_a != sha_b

    store = _FakeStore()
    _wire(monkeypatch, store, fetch_map={
        "https://example.com/a": ("ok", raw_bytes_a, "text/html", "utf-8"),
        "https://example.com/b": ("ok", raw_bytes_b, "text/html", "utf-8"),
    })

    plan = [
        {"url": "https://example.com/a", "source_url": "https://example.com/a"},
        {"url": "https://example.com/b", "source_url": "https://example.com/b"},
    ]
    result = web_fetch.capture_pages(_job(), "claimant-1", plan)

    assert result["captured"] == 2  # different source_url each -> capture_exists is False both times
    capture_a, capture_b = store.captures
    assert capture_a["captured_html_path"] == f"captures/{sha_a}.html"
    assert capture_b["captured_html_path"] == f"captures/{sha_b}.html"
    # Both rows share the SAME canonical raw_content (the dedup key's basis)
    # -- but that must never affect which raw bytes each row's path points at.
    assert capture_a["raw_content"] == capture_b["raw_content"] == text
    assert capture_a["captured_html_path"] != capture_b["captured_html_path"]


def test_capture_pages_source_url_defaults_to_url_when_absent(monkeypatch):
    store = _FakeStore()
    _wire(monkeypatch, store, fetch_map={"https://example.com/a": ("ok", b"<p>x</p>", "text/html", "utf-8")})
    web_fetch.capture_pages(_job(), "claimant-1", [{"url": "https://example.com/a"}])
    assert store.captures[0]["source_url"] == "https://example.com/a"


def test_capture_pages_honors_explicit_source_url(monkeypatch):
    store = _FakeStore()
    _wire(monkeypatch, store, fetch_map={"https://example.com/a": ("ok", b"<p>x</p>", "text/html", "utf-8")})
    plan = [{"url": "https://example.com/a", "source_url": "https://example.com/landing"}]
    web_fetch.capture_pages(_job(), "claimant-1", plan)
    assert store.captures[0]["source_url"] == "https://example.com/landing"


def test_capture_pages_no_clobber_still_mints_when_object_preexists(monkeypatch):
    """Two different source_urls fetching the SAME raw bytes (so the same
    raw_sha256 -- storage object already existing, no-clobber -> upload
    returns False) must NOT block minting a new capture row for a
    genuinely new (content_sha256, source_url) pair."""
    raw_html = "<p>Same</p>"
    raw_bytes = raw_html.encode("utf-8")
    raw_sha = _raw_sha(raw_bytes)
    store = _FakeStore(upload_exists_at={f"captures/{raw_sha}.html", f"captures/{raw_sha}.txt"})
    _wire(monkeypatch, store, fetch_map={"https://example.com/dup": ("ok", raw_bytes, "text/html", "utf-8")})

    result = web_fetch.capture_pages(_job(), "claimant-1", [{"url": "https://example.com/dup"}])

    assert result["captured"] == 1
    assert len(store.captures) == 1  # mint still happened


def test_capture_pages_sha_dedup_skips_mint_and_storage(monkeypatch):
    raw_html = "<p>Already seen</p>"
    text = canonical_text(raw_html)
    content_sha = _sha(text)
    store = _FakeStore(existing_shas={(content_sha, "https://example.com/seen")})
    _wire(monkeypatch, store, fetch_map={
        "https://example.com/seen": ("ok", raw_html.encode("utf-8"), "text/html", "utf-8"),
    })

    result = web_fetch.capture_pages(_job(), "claimant-1", [{"url": "https://example.com/seen"}])

    assert result == {"captured": 0, "unchanged": 1, "robots_skipped": 0, "errors": []}
    assert store.uploaded == []
    assert store.captures == []


def test_capture_pages_robots_denied_url_is_never_fetched(monkeypatch):
    store = _FakeStore()
    fetched_urls = []

    def _track_fetch(url, cap_bytes=None):
        fetched_urls.append(url)
        return ("ok", b"<p>x</p>", "text/html", "utf-8")

    _wire(monkeypatch, store, robots_map={"https://example.com/private": False})
    monkeypatch.setattr(web_fetch, "safe_fetch_text", _track_fetch)

    result = web_fetch.capture_pages(_job(), "claimant-1", [{"url": "https://example.com/private"}])

    assert result["robots_skipped"] == 1
    assert result["captured"] == 0
    assert fetched_urls == []  # never fetched
    assert store.uploaded == []
    assert store.captures == []


def test_capture_pages_rate_limit_cap_skips_pages_beyond_max(monkeypatch):
    store = _FakeStore()
    fetch_map = {
        "https://example.com/one": ("ok", b"<p>One</p>", "text/html", "utf-8"),
        "https://example.com/two": ("ok", b"<p>Two</p>", "text/html", "utf-8"),
    }
    fetched = []

    def _track_fetch(url, cap_bytes=None):
        fetched.append(url)
        return fetch_map.get(url)

    _wire(monkeypatch, store, fetch_map=fetch_map)
    monkeypatch.setattr(web_fetch, "safe_fetch_text", _track_fetch)

    job = _job(params={"connector": "web.fetch", "rate_limits": {"max_pages_per_domain_per_run": 1}})
    plan = [{"url": "https://example.com/one"}, {"url": "https://example.com/two"}]
    result = web_fetch.capture_pages(job, "claimant-1", plan)

    assert result["captured"] == 1
    assert fetched == ["https://example.com/one"]  # second page never fetched
    assert len(result["errors"]) == 1
    assert result["errors"][0]["skipped"] is True
    assert "max_pages_per_domain_per_run" in result["errors"][0]["reason"]


def test_capture_pages_lease_lost_mid_plan_aborts_remaining_pages(monkeypatch):
    store = _FakeStore()
    fetch_map = {
        "https://example.com/one": ("ok", b"<p>One</p>", "text/html", "utf-8"),
        "https://example.com/two": ("ok", b"<p>Two</p>", "text/html", "utf-8"),
        "https://example.com/three": ("ok", b"<p>Three</p>", "text/html", "utf-8"),
    }
    fetched = []

    def _track_fetch(url, cap_bytes=None):
        fetched.append(url)
        return fetch_map.get(url)

    # Each page that fully succeeds now consumes THREE assert_lease calls
    # (top-of-loop, pre-fetch, pre-write -- FIX 4's two new fences). Pages
    # "one" and "two" hold the lease through all three checks each (6
    # Trues); page "three"'s TOP-of-loop check is the 7th call and returns
    # False, aborting the rest of the plan.
    _wire(monkeypatch, store, fetch_map=fetch_map, lease_values=[True] * 6 + [False])
    monkeypatch.setattr(web_fetch, "safe_fetch_text", _track_fetch)

    plan = [{"url": u} for u in fetch_map]
    result = web_fetch.capture_pages(_job(), "claimant-1", plan)

    assert result["captured"] == 2
    assert fetched == ["https://example.com/one", "https://example.com/two"]
    assert "https://example.com/three" not in fetched
    assert len(store.captures) == 2


def test_capture_pages_lease_lost_before_fetch_skips_page_without_fetching(monkeypatch):
    """FIX 4, fence #2: losing the lease AFTER the robots/rate-limit checks
    but BEFORE the actual fetch must skip this page entirely (never call
    safe_fetch_text -- nothing has been written yet, so there is nothing to
    undo) while letting the REST of the plan proceed normally."""
    store = _FakeStore()
    fetch_map = {
        "https://example.com/skip-me": ("ok", b"<p>Skip</p>", "text/html", "utf-8"),
        "https://example.com/fine": ("ok", b"<p>Fine</p>", "text/html", "utf-8"),
    }
    fetched = []

    def _track_fetch(url, cap_bytes=None):
        fetched.append(url)
        return fetch_map.get(url)

    # Page "skip-me": top=True, pre-fetch=False (lease lost here) -> 2 calls.
    # Page "fine": top=True, pre-fetch=True, pre-write=True -> 3 calls, all
    # True, succeeds normally.
    _wire(monkeypatch, store, fetch_map=fetch_map, lease_values=[True, False, True, True, True])
    monkeypatch.setattr(web_fetch, "safe_fetch_text", _track_fetch)

    plan = [{"url": "https://example.com/skip-me"}, {"url": "https://example.com/fine"}]
    result = web_fetch.capture_pages(_job(), "claimant-1", plan)

    assert fetched == ["https://example.com/fine"]  # skip-me's fetch never ran
    assert result["captured"] == 1
    assert {"url": "https://example.com/skip-me", "reason": "lease_lost", "skipped": True} in result["errors"]
    assert all("skip-me" not in path for path, _data, _ct in store.uploaded)


def test_capture_pages_lease_lost_before_write_skips_upload_and_mint(monkeypatch):
    """FIX 4, fence #3: losing the lease AFTER a successful fetch but BEFORE
    the uploads+mint block must skip the write entirely -- NO upload, NO
    mint, for this page -- while the REST of the plan still proceeds (only
    the top-of-loop check aborts the whole plan)."""
    store = _FakeStore()
    fetch_map = {
        "https://example.com/one": ("ok", b"<p>One</p>", "text/html", "utf-8"),
        "https://example.com/two": ("ok", b"<p>Two</p>", "text/html", "utf-8"),
    }
    fetched = []

    def _track_fetch(url, cap_bytes=None):
        fetched.append(url)
        return fetch_map.get(url)

    # Page "one": top=True, pre-fetch=True (fetch happens), pre-write=False
    # (lease lost right before the write -- skip upload+mint). Page "two":
    # top=True, pre-fetch=True, pre-write=True -- succeeds normally,
    # proving the plan continued.
    _wire(monkeypatch, store, fetch_map=fetch_map, lease_values=[True, True, False, True, True, True])
    monkeypatch.setattr(web_fetch, "safe_fetch_text", _track_fetch)

    plan = [{"url": "https://example.com/one"}, {"url": "https://example.com/two"}]
    result = web_fetch.capture_pages(_job(), "claimant-1", plan)

    assert fetched == ["https://example.com/one", "https://example.com/two"]  # BOTH were fetched
    assert result["captured"] == 1  # only page "two" was minted
    assert len(store.captures) == 1
    assert store.captures[0]["url"] == "https://example.com/two"
    # Page "one" never uploaded anything (skipped before storage-first block).
    assert len(store.uploaded) == 2  # only page two's html+text
    one_raw_sha = _raw_sha(b"<p>One</p>")
    assert all(one_raw_sha not in path for path, _d, _ct in store.uploaded)
    assert {"url": "https://example.com/one", "reason": "lease_lost", "skipped": True} in result["errors"]


def test_capture_pages_fetch_failure_records_error_and_continues(monkeypatch):
    store = _FakeStore()
    fetch_map = {
        "https://example.com/bad": None,  # safe_fetch_text failed (SSRF/timeout/etc.)
        "https://example.com/good": ("ok", b"<p>Good</p>", "text/html", "utf-8"),
    }
    _wire(monkeypatch, store, fetch_map=fetch_map)

    plan = [{"url": "https://example.com/bad"}, {"url": "https://example.com/good"}]
    result = web_fetch.capture_pages(_job(), "claimant-1", plan)

    assert result["captured"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0] == {"url": "https://example.com/bad", "reason": "fetch_failed"}


def test_capture_pages_one_page_exception_does_not_abort_the_plan(monkeypatch):
    store = _FakeStore(raise_on_url={"https://example.com/boom": RuntimeError("db is down")})
    fetch_map = {
        "https://example.com/boom": ("ok", b"<p>Boom</p>", "text/html", "utf-8"),
        "https://example.com/fine": ("ok", b"<p>Fine</p>", "text/html", "utf-8"),
    }
    _wire(monkeypatch, store, fetch_map=fetch_map)

    plan = [{"url": "https://example.com/boom"}, {"url": "https://example.com/fine"}]
    result = web_fetch.capture_pages(_job(), "claimant-1", plan)

    assert result["captured"] == 1  # the second page still succeeded
    assert len(result["errors"]) == 1
    assert result["errors"][0]["url"] == "https://example.com/boom"
    assert "db is down" in result["errors"][0]["reason"]


def test_capture_pages_missing_url_records_error_without_touching_store(monkeypatch):
    store = _FakeStore()
    _wire(monkeypatch, store)

    result = web_fetch.capture_pages(_job(), "claimant-1", [{"source_url": "https://example.com/no-url"}])

    assert result["errors"] == [{"url": None, "reason": "missing url"}]
    assert store.calls == []


def test_capture_pages_empty_plan_is_a_clean_noop(monkeypatch):
    store = _FakeStore()
    _wire(monkeypatch, store)
    result = web_fetch.capture_pages(_job(), "claimant-1", [])
    assert result == {"captured": 0, "unchanged": 0, "robots_skipped": 0, "errors": []}
    assert store.collection_runs == [("run-1", "done", 0)]


def test_capture_pages_records_collection_run_partial_on_errors(monkeypatch):
    store = _FakeStore()
    _wire(monkeypatch, store, fetch_map={"https://example.com/bad": None})
    web_fetch.capture_pages(_job(), "claimant-1", [{"url": "https://example.com/bad"}])
    assert store.collection_runs == [("run-1", "partial", 0)]


def test_capture_pages_records_collection_run_aborted_on_lease_loss(monkeypatch):
    store = _FakeStore()
    _wire(monkeypatch, store, lease_values=[False])
    web_fetch.capture_pages(_job(), "claimant-1", [{"url": "https://example.com/a"}])
    assert store.collection_runs == [("run-1", "aborted", 0)]


# ---------------------------------------------------------------------------
# FIX A (Sol review round 2): the stored '.html' object and its sha256 key
# must be the TRUE raw response bytes -- never a decode-then-re-encode
# round trip, which can change the bytes (and thus the hash) even though
# `errors="replace"` never raises.
# ---------------------------------------------------------------------------

def test_capture_pages_stores_true_raw_bytes_not_reencoded_text(monkeypatch):
    """An invalid UTF-8 byte in the raw response decodes (with
    errors='replace') to a U+FFFD replacement character -- re-encoding THAT
    decoded text as UTF-8 produces a DIFFERENT byte string than the true raw
    bytes. The stored html object (and the sha256 that names its path) must
    come from the ORIGINAL bytes, never from re-encoding the decoded text."""
    raw_bytes = b"<p>bad byte \xff here</p>"
    charset = "utf-8"
    reencoded = raw_bytes.decode(charset, errors="replace").encode(charset)
    assert reencoded != raw_bytes  # sanity: the round trip really does differ

    true_raw_sha = _raw_sha(raw_bytes)
    reencoded_sha = _raw_sha(reencoded)
    assert true_raw_sha != reencoded_sha

    store = _FakeStore()
    _wire(monkeypatch, store, fetch_map={
        "https://example.com/a": ("ok", raw_bytes, "text/html", charset),
    })

    result = web_fetch.capture_pages(_job(), "claimant-1", [{"url": "https://example.com/a"}])

    assert result["captured"] == 1
    html_path, uploaded_bytes, content_type = store.uploaded[0]
    assert html_path == f"captures/{true_raw_sha}.html"  # keyed by the TRUE raw sha
    assert uploaded_bytes == raw_bytes                    # the TRUE raw bytes, unmodified
    assert uploaded_bytes != reencoded                    # never the round-tripped bytes
    assert content_type == "text/html"


# ---------------------------------------------------------------------------
# FIX B (Sol review round 2): a redirect returned by safe_fetch_text is
# followed ONE hop, ONLY same-host, ONLY if robots/rate-limit still allow
# the target -- everything else is a "redirect_not_followed:<why>" gap note.
# ---------------------------------------------------------------------------

def test_capture_pages_follows_same_host_redirect_checking_robots_and_rate_limit(monkeypatch):
    store = _FakeStore()
    robots_calls = []

    def _fake_robots_allowed(url, cache, limiter):
        robots_calls.append(url)
        return True

    fetch_sequence = {
        "https://example.com/start": ("redirect", "https://example.com/final"),
        "https://example.com/final": ("ok", b"<p>Final</p>", "text/html", "utf-8"),
    }
    fetched_urls = []

    def _track_fetch(url, cap_bytes=None):
        fetched_urls.append(url)
        return fetch_sequence[url]

    _wire(monkeypatch, store)
    monkeypatch.setattr(web_fetch.base, "robots_allowed", _fake_robots_allowed)
    monkeypatch.setattr(web_fetch, "safe_fetch_text", _track_fetch)

    result = web_fetch.capture_pages(_job(), "claimant-1", [{"url": "https://example.com/start"}])

    assert fetched_urls == ["https://example.com/start", "https://example.com/final"]
    assert robots_calls == ["https://example.com/start", "https://example.com/final"]  # BOTH checked
    assert result["captured"] == 1
    final_sha = _raw_sha(b"<p>Final</p>")
    assert store.captures[0]["captured_html_path"] == f"captures/{final_sha}.html"


def test_capture_pages_cross_host_redirect_is_not_followed_and_records_gap_note(monkeypatch):
    store = _FakeStore()
    fetched_urls = []

    def _track_fetch(url, cap_bytes=None):
        fetched_urls.append(url)
        if url == "https://example.com/start":
            return ("redirect", "https://evil.example.org/steal")
        raise AssertionError(f"must never fetch the cross-host redirect target: {url}")

    _wire(monkeypatch, store)
    monkeypatch.setattr(web_fetch, "safe_fetch_text", _track_fetch)

    result = web_fetch.capture_pages(_job(), "claimant-1", [{"url": "https://example.com/start"}])

    assert fetched_urls == ["https://example.com/start"]  # target never fetched
    assert result["captured"] == 0
    assert result["errors"] == [{
        "url": "https://evil.example.org/steal",
        "reason": "redirect_not_followed:cross_host",
        "skipped": True,
    }]


def test_capture_pages_second_redirect_is_not_followed_and_records_gap_note(monkeypatch):
    store = _FakeStore()
    fetch_sequence = {
        "https://example.com/start": ("redirect", "https://example.com/hop2"),
        "https://example.com/hop2": ("redirect", "https://example.com/hop3"),
    }
    fetched_urls = []

    def _track_fetch(url, cap_bytes=None):
        fetched_urls.append(url)
        return fetch_sequence[url]

    _wire(monkeypatch, store)
    monkeypatch.setattr(web_fetch, "safe_fetch_text", _track_fetch)

    result = web_fetch.capture_pages(_job(), "claimant-1", [{"url": "https://example.com/start"}])

    assert fetched_urls == ["https://example.com/start", "https://example.com/hop2"]  # hop3 never fetched
    assert result["captured"] == 0
    assert result["errors"] == [{
        "url": "https://example.com/hop3",
        "reason": "redirect_not_followed:second_redirect",
        "skipped": True,
    }]


def test_capture_pages_robots_denied_redirect_target_records_gap_note(monkeypatch):
    """A same-host redirect target that robots.txt denies must NOT be
    fetched -- and must be recorded as a `redirect_not_followed:<why>` gap
    note, DISTINCT from the top-level page's own `robots_skipped` counter
    (which stays 0 here: the ORIGINAL page url was itself allowed)."""
    store = _FakeStore()
    fetched_urls = []

    def _track_fetch(url, cap_bytes=None):
        fetched_urls.append(url)
        return ("redirect", "https://example.com/private")

    _wire(monkeypatch, store, robots_map={"https://example.com/private": False})
    monkeypatch.setattr(web_fetch, "safe_fetch_text", _track_fetch)

    result = web_fetch.capture_pages(_job(), "claimant-1", [{"url": "https://example.com/start"}])

    assert fetched_urls == ["https://example.com/start"]  # redirect target never fetched
    assert result["robots_skipped"] == 0  # the ORIGINAL page was allowed
    assert result["errors"] == [{
        "url": "https://example.com/private",
        "reason": "redirect_not_followed:robots_denied",
        "skipped": True,
    }]


def test_capture_pages_treats_case_and_default_port_variant_redirect_as_same_host(monkeypatch):
    """FIX D wired into FIX B: a redirect target that differs from the
    source only by case or an explicit-default-port -- which canonical_host
    normalizes away -- must still be treated as a SAME-host redirect (and
    thus followed), not rejected as cross-host."""
    store = _FakeStore()
    fetch_sequence = {
        "https://example.com/start": ("redirect", "https://EXAMPLE.com:443/final"),
        "https://EXAMPLE.com:443/final": ("ok", b"<p>Final</p>", "text/html", "utf-8"),
    }
    fetched_urls = []

    def _track_fetch(url, cap_bytes=None):
        fetched_urls.append(url)
        return fetch_sequence[url]

    _wire(monkeypatch, store)
    monkeypatch.setattr(web_fetch, "safe_fetch_text", _track_fetch)

    result = web_fetch.capture_pages(_job(), "claimant-1", [{"url": "https://example.com/start"}])

    assert fetched_urls == ["https://example.com/start", "https://EXAMPLE.com:443/final"]
    assert result["captured"] == 1
