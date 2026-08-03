"""web.fetch connector (Phase 2 of the deep-research plan): a static-HTML
site-capture connector. No paid API call is ever made on this path --
`settings.price_cards["scrape"]["web.fetch"]` is priced at 0 cents precisely
because this is a model-free fetch -- so, unlike the ads connector,
`capture_pages` does not call `worker.budget.reserve()`/`settle()` at all:
those exist to guard a PAID call's worst-case ceiling against a project's
budget, and there is no such call here to guard (see the module-level note
in `worker.jobs._dispatch`'s web.fetch branch for the same reasoning).
`worker.spend_guard` is, by its own module docstring, an ANTHROPIC spend
guard specifically -- it has nothing to say about a plain HTTP GET, so it is
correctly never invoked here either.

`capture_pages` is the `(job, claimant, plan) -> dict` entrypoint
`worker.jobs._dispatch` routes (job_kind='scrape', connector='web.fetch') to.
"""
import hashlib
import logging

from worker import jobs, store
from worker.canonical import canonical_text
from worker.config import settings
from worker.connectors import base
from worker.net import canonical_host, safe_fetch_text

logger = logging.getLogger(__name__)

_HTML_BUCKET_PREFIX = "captures"


def _decode_raw(raw_bytes: bytes, charset: str) -> str:
    """Decode `raw_bytes` for canonicalization using the `charset`
    `safe_fetch_text` detected (FIX A, Sol review round 2 -- decode ownership
    moved here from `worker.net`, which now hands back undecoded bytes so
    the STORED object and its hash are the true raw bytes, never a
    decode-then-re-encode copy). Falls back to utf-8/replace on an
    unrecognised codec name -- a decode must never raise and crash the
    connector, mirroring the exact fallback `worker.net.safe_fetch_text`
    used to apply internally before this fix."""
    charset = charset or "utf-8"
    try:
        return raw_bytes.decode(charset, errors="replace")
    except LookupError:  # unrecognised encoding name -- fall back rather than raise
        return raw_bytes.decode("utf-8", errors="replace")


def _follow_one_hop_redirect(source_url, target, job_id, claimant, robots_cache, limiter, result):
    """FIX B (Sol review round 2): `capture_pages` calls this exactly once
    per page, only after its OWN fetch of the plan's url came back
    `("redirect", target)`. Follows `target` -- and ONLY `target`, never a
    second hop past it -- under the exact same discipline a normal page
    fetch gets: its own robots.txt check, its own per-domain rate-limit
    check (domain keyed via FIX D's `canonical_host`, consistently with
    every other domain key in this connector), and the SAME lease fence
    (FIX 4) immediately before the fetch actually happens.

    Returns `("ok", raw_bytes, content_type, charset)` if `target` was
    fetched successfully, or `None` if the redirect was not followed (or the
    target's own fetch failed) -- in EVERY `None` case this function has
    already appended the explanatory entry to `result["errors"]` itself, so
    the caller's only job on a `None` return is `continue`.

    A redirect is followed only if ALL of:
      - `target`'s host canonicalizes to the SAME host as `source_url` -- a
        cross-host redirect is a materially different trust decision
        (leaving the domain this page's plan actually named) and is never
        followed automatically, no matter how innocuous-looking the target.
      - `base.robots_allowed(target, ...)` -- the target gets its own
        robots.txt check, exactly like any other URL this connector fetches.
      - `limiter.allow(...)` for the target's domain -- the target still
        counts against (and is still gated by) that domain's per-run page
        cap, exactly like a normal page fetch.
      - the lease is still held immediately before `target` is actually
        fetched.

    Any other outcome -- cross-host, a SECOND redirect out of the target's
    own fetch, robots-denied, or rate-limited -- is recorded as ONE
    `{"url": target, "reason": "redirect_not_followed:<why>", "skipped":
    True}` entry: deliberately a DIFFERENT reason shape than the top-level
    page's own `robots_skipped` counter / rate-limit error, so a caller can
    tell "the ORIGINAL page was skipped" apart from "a redirect it hit was
    not followed". A genuine fetch failure on the target (a network error,
    bad content-type, over-cap, ...) is still recorded as the ordinary
    `{"url": target, "reason": "fetch_failed"}` -- that failure mode is no
    different here than for any other fetch.
    """
    source_host = canonical_host(source_url)
    target_host = canonical_host(target)
    if target_host != source_host:
        result["errors"].append(
            {"url": target, "reason": "redirect_not_followed:cross_host", "skipped": True}
        )
        return None

    if not base.robots_allowed(target, robots_cache, limiter):
        result["errors"].append(
            {"url": target, "reason": "redirect_not_followed:robots_denied", "skipped": True}
        )
        return None

    if not limiter.allow(target_host):
        result["errors"].append(
            {"url": target, "reason": "redirect_not_followed:rate_limited", "skipped": True}
        )
        return None

    # FIX 4, fence #2's discipline, re-applied to this hop (FIX B): a
    # revoked worker must not even START fetching the redirect target.
    if not jobs.assert_lease(job_id, claimant):
        logger.warning(
            "web_fetch.capture_pages: lease lost for job %s (claimant %s) "
            "just before fetching redirect target %s -- skipping this page",
            job_id, claimant, target,
        )
        result["errors"].append({"url": target, "reason": "lease_lost", "skipped": True})
        return None

    limiter.wait(target_host)
    try:
        fetched = safe_fetch_text(target, cap_bytes=settings.web_fetch_cap_bytes)
    finally:
        limiter.record(target_host)

    if fetched is None:
        result["errors"].append({"url": target, "reason": "fetch_failed"})
        return None

    if fetched[0] == "redirect":
        second_target = fetched[1]
        result["errors"].append(
            {"url": second_target, "reason": "redirect_not_followed:second_redirect", "skipped": True}
        )
        return None

    return fetched


def capture_pages(job: dict, claimant: str, plan: list) -> dict:
    """Fetch every `{url, source_url?}` entry in `plan` and mint one
    `research_site_captures` row per genuinely new page.

    Per-page order (do not reorder without re-reading the reasoning below):

      1. `jobs.assert_lease(job_id, claimant)` -- the lease fence. False
         means the reaper has already re-queued this job for a new
         claimant; STOP processing the rest of `plan` entirely (not just
         this page) rather than racing that new claimant's own run.
      2. `base.robots_allowed(url, robots_cache, limiter)` -- a
         robots-denied URL is SKIPPED: never fetched, and recorded
         (`robots_skipped` counter + an info log line naming the URL)
         rather than silently dropped. The robots.txt lookup itself goes
         through the SAME `limiter` page fetches use (FIX 5 -- see
         `base._get_robots`).
      3. `limiter.allow(domain)` -- the per-domain page cap
         (`max_pages_per_domain_per_run`). Reached -> skip and record, same
         as a robots skip, without ever calling `wait()`/fetching.
      4. A SECOND `jobs.assert_lease` check, immediately before the fetch
         (FIX 4): the top-of-loop check (step 1) only proves the lease was
         held when THIS page's iteration began; a long-running earlier page
         in the same plan could have let it expire since. Loss here just
         SKIPS this one page (`continue`) -- nothing has been written yet,
         so there is nothing to undo, and the top-of-loop check on the next
         iteration will catch (and abort on) a lease that stays lost.
      5. `limiter.wait(domain)` then `safe_fetch_text(url)` inside a `try`
         whose `finally` calls `limiter.record(domain)` (FIX 5) -- so an
         exception mid-fetch still consumes the domain's rate-limit window
         and page-cap budget, exactly as a normal fetch would.
         `safe_fetch_text` returns `("ok", raw_bytes, content_type,
         charset)`, `("redirect", target)`, or `None` (FIX B, Sol review
         round 2). A `("redirect", target)` is handed to
         `_follow_one_hop_redirect`, which re-runs robots/rate-limit/lease
         checks against `target` and follows it ONLY if it is same-host,
         robots-allowed, and within the domain's page cap -- see that
         function's own docstring. `None` either way (the original fetch, or
         the followed redirect hop) records `fetch_failed` and skips the
         page.
      6. `_decode_raw(raw_bytes, charset)` then `canonical_text(...)` -- the
         deterministic text extraction, now over bytes DECODED BY THE
         CONNECTOR (FIX A, Sol review round 2: `worker.net.safe_fetch_text`
         no longer decodes -- it hands back the true, undecoded response
         bytes, so this connector is the one place that both hashes/stores
         the raw bytes AND decodes them for canonicalization, and those two
         uses can never silently diverge).
      7. `sha256(raw_bytes)` (`raw_sha256`, of the TRUE raw response bytes --
         FIX A) and, separately, `sha256(canonical_text)` (`content_sha256`)
         -- see FIX 3 below. `content_sha256` is computed HERE only to drive
         this connector's own dedup decision; `rs_create_site_capture`
         independently (re-)computes `content_sha256` server-side from the
         raw_content it is given, so the two are guaranteed to agree as long
         as this function always passes the SAME canonical text it hashed
         (which it does below).
      8. `store.capture_exists(content_sha256, source_url)` -- idempotent-
         skip check, UNCHANGED and still canonical-text-based (change
         detection stays "did the rendered content change", not "did the
         raw bytes change"). True -> record 'unchanged' and move on WITHOUT
         touching storage or minting anything.
      9. A THIRD `jobs.assert_lease` check, immediately before the
         uploads+mint block (FIX 4): loss here means a revoked worker must
         not perform ANY external write -- skip both uploads and the mint
         entirely (`continue`), never a partial write.
      10. Storage-first, keyed by `raw_sha256` (FIX 3 -- NOT
          `content_sha256`): `store.upload_capture_object` for the raw HTML
          at `captures/<raw_sha256>.html`, THEN the canonical text at
          `captures/<raw_sha256>.txt`, both NO-CLOBBER (an object that
          already exists at that path -- e.g. from an earlier page in this
          same run, or a past run, whose raw bytes happened to hash
          identically -- is left untouched, never overwritten). Keying the
          HTML object by the hash of its OWN raw bytes (rather than the
          canonical-text hash, which two DIFFERENT raw pages can share)
          guarantees a capture row's `captured_html_path` always points at
          ITS OWN raw bytes, never an earlier, different page's. ONLY THEN
          `store.create_site_capture(..., captured_html_path=<the html
          object's path>)` -- the deployed RPC itself REQUIRES
          `p_captured_html_path` and raises if it's null/blank, so a
          capture row must never be mintable before its bytes are durably
          stored; this ordering is what makes that guarantee true in this
          connector's own code, not just in the DB constraint.

    Returns `{"captured": int, "unchanged": int, "robots_skipped": int,
    "errors": list[dict]}`. `errors` entries are `{"url", "reason"}` (plus
    `"skipped": True` for a non-fetch skip -- a per-domain cap hit OR a
    lease lost mid-page, so a caller can tell "we chose not to fetch/write
    this" apart from "we tried and failed"). A single page's exception
    (fetch/storage/mint) is caught and recorded as one more `errors` entry --
    it can never abort the rest of the plan; only a lost lease at the TOP of
    a page's iteration (step 1) does that.
    """
    job_id = job.get("id")
    params = job.get("params") or {}
    competitor_id = job.get("competitor_id") or params.get("competitor_id")
    rate_limits = params.get("rate_limits") or settings.web_fetch_default_rate_limits
    connector = "web.fetch"

    robots_cache: dict = {}
    limiter = base.DomainRateLimiter(rate_limits)
    result = {"captured": 0, "unchanged": 0, "robots_skipped": 0, "errors": []}

    run_id = store.start_collection_run(connector)
    run_status = "done"

    try:
        for index, page in enumerate(plan):
            url = (page or {}).get("url")
            source_url = (page or {}).get("source_url") or url
            if not url:
                result["errors"].append({"url": url, "reason": "missing url"})
                continue

            if not jobs.assert_lease(job_id, claimant):
                logger.warning(
                    "web_fetch.capture_pages: lease lost for job %s (claimant %s) -- "
                    "aborting remaining plan (%d of %d pages left)",
                    job_id, claimant, len(plan) - index, len(plan),
                )
                run_status = "aborted"
                break

            domain = canonical_host(url)

            try:
                if not base.robots_allowed(url, robots_cache, limiter):
                    result["robots_skipped"] += 1
                    logger.info("web_fetch: robots.txt disallows %s -- skipping", url)
                    continue

                if not limiter.allow(domain):
                    result["errors"].append(
                        {"url": url, "reason": "max_pages_per_domain_per_run reached", "skipped": True}
                    )
                    continue

                # FIX 4, fence #2: a revoked worker must not even START a
                # new fetch. Nothing has been written yet, so losing the
                # lease here just skips this one page -- the top-of-loop
                # check (fence #1) aborts the whole plan once it re-runs.
                if not jobs.assert_lease(job_id, claimant):
                    logger.warning(
                        "web_fetch.capture_pages: lease lost for job %s (claimant %s) "
                        "just before fetching %s -- skipping this page",
                        job_id, claimant, url,
                    )
                    result["errors"].append({"url": url, "reason": "lease_lost", "skipped": True})
                    continue

                limiter.wait(domain)
                try:
                    fetched = safe_fetch_text(url, cap_bytes=settings.web_fetch_cap_bytes)
                finally:
                    # FIX 5: record() runs even if safe_fetch_text raised --
                    # an attempted request still consumed the domain's
                    # rate-limit window and page-cap budget.
                    limiter.record(domain)

                if fetched is None:
                    result["errors"].append({"url": url, "reason": "fetch_failed"})
                    continue

                if fetched[0] == "redirect":
                    # FIX B: the ORIGINAL fetch redirected -- decide whether
                    # to follow it (same-host, robots-allowed, within the
                    # domain's page cap) via the dedicated helper, which has
                    # already recorded a gap note on any `None` return.
                    fetched = _follow_one_hop_redirect(
                        url, fetched[1], job_id, claimant, robots_cache, limiter, result,
                    )
                    if fetched is None:
                        continue

                _, raw_bytes, _content_type, charset = fetched
                # FIX A: hash and store the TRUE raw response bytes --
                # decoding (for canonicalization only) happens AFTER the
                # hash, over a separate decoded copy, so the stored object
                # and its sha256 key are never a decode-then-re-encode
                # round trip that could silently change the bytes.
                text = canonical_text(_decode_raw(raw_bytes, charset))
                # FIX 3: two distinct hashes with two distinct jobs.
                # `raw_sha256` (of the RAW bytes) drives the STORAGE path --
                # a capture row's captured_html_path must always point at
                # its OWN raw bytes, and two different raw pages can share
                # one canonical text. `content_sha256` (of the CANONICAL
                # text, unchanged from before this fix) stays the dedup/
                # change-detection key the RPC also computes server-side.
                raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
                content_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()

                if store.capture_exists(content_sha256, source_url):
                    result["unchanged"] += 1
                    continue

                # FIX 4, fence #3: a revoked worker must not perform ANY
                # external write. Loss here skips BOTH uploads and the
                # mint -- never a partial write.
                if not jobs.assert_lease(job_id, claimant):
                    logger.warning(
                        "web_fetch.capture_pages: lease lost for job %s (claimant %s) "
                        "just before uploading/minting %s -- skipping the write",
                        job_id, claimant, url,
                    )
                    result["errors"].append({"url": url, "reason": "lease_lost", "skipped": True})
                    continue

                # .html is keyed by the RAW-bytes sha (so captured_html_path
                # always points at THIS row's exact bytes). .txt is keyed by the
                # CANONICAL-text sha (content_sha256) so the stored canonical
                # text is content-addressed by its OWN hash -- two responses with
                # identical bytes but different charset headers can decode to
                # different canonical text, and keying the .txt by raw-sha would
                # let no-clobber leave a .txt inconsistent with a row's canonical
                # text (Sol review round 3).
                html_path = f"{_HTML_BUCKET_PREFIX}/{raw_sha256}.html"
                text_path = f"{_HTML_BUCKET_PREFIX}/{content_sha256}.txt"

                # Storage-first, no-clobber: see step 10 above. Neither
                # upload's return value gates the mint below -- False just
                # means the object already existed (fine; its content is
                # already correct, since its path IS the hash of its own
                # bytes).
                store.upload_capture_object(html_path, raw_bytes, "text/html")
                store.upload_capture_object(text_path, text.encode("utf-8"), "text/plain")

                capture_id = store.create_site_capture(
                    url=url,
                    source_url=source_url,
                    raw_content=text,
                    connector=connector,
                    competitor_id=competitor_id,
                    captured_html_path=html_path,
                )
                result["captured"] += 1
                logger.info(
                    "web_fetch: captured %s -> research_site_captures %s "
                    "(raw_sha=%s content_sha=%s)",
                    url, capture_id, raw_sha256[:12], content_sha256[:12],
                )
            except Exception as exc:  # noqa: BLE001 -- one bad page must never fail the whole plan
                logger.warning("web_fetch: error capturing %s: %s", url, exc)
                result["errors"].append({"url": url, "reason": str(exc)})
    finally:
        if run_status == "done" and result["errors"]:
            # Not every error is fatal to the run as a whole (a handful of
            # skipped/failed pages alongside real captures is a partial,
            # not a failed, run) -- but the collection-run record should
            # still be honest that something went imperfectly, not a bare
            # 'done'.
            run_status = "partial"
        store.finish_collection_run(run_id, run_status, records=result["captured"])

    return result
