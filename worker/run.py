"""Research Studio worker entrypoint.

    python -m worker.run "MeUndies" [domain]

Scrapes a competitor's live Meta ads, analyzes them server-side with Claude
(vision + structured output), and persists a snapshot + analysis to Supabase.
Requires the server-side env (see .env.example): ANTHROPIC_API_KEY,
SCRAPECREATORS_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY.

Task 1.6/1.7 (deep-research job spine): `main()` no longer runs the ads
orchestration directly -- it enqueues one `scrape`/`ad_library.
scrapecreators` `research_jobs` row (idempotent per brand/day, R4) and runs
EXACTLY that one job via `jobs.run_one()` (P1-4/P1-5 fix: no longer the
whole-queue `drain(..., once=True)`, which would also claim and fail every
other queued kind this CLI has no handler for yet). The orchestration
itself lives, UNCHANGED, in `run_ads()` below (an "extract method" refactor
of the former `main()` body); `handle_scrape()` is the thin adapter
`worker.jobs._dispatch()` routes to. See each function's docstring for the
full contract.
"""
import logging
import sys
import traceback
from datetime import datetime
from typing import Optional

from worker import ingest, analyze, jobs, store
from worker.config import settings
from worker.spend_guard import ResearchSpendCapExceeded

logger = logging.getLogger(__name__)


def run_ads(
    brand: str,
    domain: Optional[str] = None,
    job_id: Optional[str] = None,
    claimant: Optional[str] = None,
) -> None:
    """The full single-shot ads orchestration -- UNCHANGED from the former
    `main()` body (Task 1.6 is a pure extract-method refactor; no ads logic
    changes here). pin/resolve -> pull_ads -> dedup -> save_snapshot ->
    select_for_analysis -> fetch_images -> analyze.analyze() ->
    save_analysis -> finish_run, all inside one try/finally, exactly as
    before.

    `job_id`/`claimant` are new, OPTIONAL parameters: when both are given
    (the job-spine path, via `handle_scrape`), this adds TWO defensive
    lease-fence checks around the one paid call this function makes --
    `jobs.assert_lease(job_id, claimant)` immediately BEFORE the analysis
    spend, and again immediately AFTER `analyze()` returns and BEFORE
    `store.save_analysis()` (P1-1/P1-2 fix) -- as a job-spine-specific layer
    alongside `analyze.analyze()`'s own `spend_guard.guard()` (Layer 1,
    unconditional, unchanged). This analysis call can legitimately run for
    minutes, so a lease revoked by the reaper mid-run (this same worker
    process presumably dead or wedged) is caught here rather than spending
    against, or WRITING for, a job the reaper has already re-queued for
    someone else. The pre-spend check aborts before the (unavoidable) spend
    happens at all; the post-analysis check can no longer prevent the
    spend -- it already happened -- but it DOES prevent this now-stale
    result from being written over whatever the new claimant's own re-run
    has since saved. P3-P4 multi-call handlers (collect/verify/synthesize)
    must assert_lease before EVERY paid call they make; this single-call
    ads handler only ever makes the one call, so it fences immediately
    before and immediately after it. When either `job_id` or `claimant` is
    None (no job-spine context -- direct/legacy call), both checks are
    skipped entirely and the function behaves exactly as the old `main()`
    did.
    """
    comp_id = store.get_or_create_competitor(brand, domain)
    # Record the run as STARTED before any expensive work (pull_ads/analysis), so a
    # run stuck mid-flight (crash, hang, spend cap trip) is visible in research_runs
    # rather than invisible until save_analysis lands at the end (the 2026-07 cost
    # incident: nothing was written until the terminal save_analysis). Best-effort —
    # store.start_run never raises; run_id is None if the registry write failed.
    run_id = store.start_run(brand, comp_id)

    # Terminal state for finish_run, built up as the run progresses and recorded
    # in the `finally` below NO MATTER how the run ends (early return, spend cap,
    # any exception -- including one raised by save_analysis itself). This is the
    # one and only finish_run call for the run; do not add another.
    status = "failed"
    err: Optional[str] = None
    est_calls = 0
    scraped: Optional[int] = None
    analyzed: Optional[int] = None
    claude_attempted = False
    snap_id: Optional[str] = None
    # Placeholder meta for the (rare) case an exception hits before the real
    # meta dict below is built, so the except-Exception handler always has
    # something to write into the observability row.
    meta = {"model": settings.model, "distinct_ads": 0, "images_analyzed": 0, "scraped_ads": 0}

    try:
        # Pin-first: a previously pinned/observed platform_id in
        # research_page_identities is authoritative and skips the fuzzy
        # search entirely (the fuzzy match is what mis-resolved "Lounge" to
        # an unrelated Chilean retailer and left "Secret Coco"/"Hunkemoller"
        # on empty pages). Fuzzy fallback only when nothing is pinned yet,
        # and every fuzzy resolution is captured so the next run for this
        # brand is pinned automatically.
        platform_id = store.get_pinned_platform_id(comp_id)
        if platform_id:
            logger.info("using pinned platform_id '%s' for '%s'", platform_id, brand)
        else:
            platform_id = ingest.resolve_platform_id(brand)
            store.record_page_identity(comp_id, platform_id, None, "observed")
        # Pull wide + dedup; the SNAPSHOT keeps everything (spec §9). The expensive
        # analysis runs on a scope-aware sample (active-first, recency+longevity).
        raw = ingest.dedup(ingest.pull_ads(platform_id))
        snap_id = store.save_snapshot(comp_id, platform_id, raw)
        scraped = len(raw)

        # Guard: a resolved page with no active ads (e.g. a brand page whose ads run
        # under a replacement/"II" or persona identity) shouldn't burn an Anthropic
        # call on an empty analysis. Record it observably and stop.
        if not raw:
            status, est_calls = "no_ads", 0
            analyzed = 0
            store.save_analysis(
                comp_id, snap_id, {},
                {"model": settings.model, "distinct_ads": 0, "images_analyzed": 0, "scraped_ads": 0},
                status="no_ads",
                error="Resolved page returned no active ads — try the exact ad-running page name.",
            )
            print(f"no ads for {brand}: resolved page has no active ads — try the exact ad-running page name", file=sys.stderr)
            return

        sample = ingest.select_for_analysis(raw)
        analyzed = len(sample)

        meta = {
            "model": settings.model,
            "distinct_ads": len(sample),
            "images_analyzed": 0,
            "scraped_ads": len(raw),
        }

        images = ingest.fetch_images(sample, cap=settings.max_images)
        meta["images_analyzed"] = len(images)
        # Defense-in-depth lease fence (Task 1.6, decision C): only active
        # when this run was dispatched via the job spine (job_id/claimant
        # both given). analyze()'s own spend_guard.guard() is Layer 1 and
        # always runs regardless -- this is an ADDITIONAL, job-spine-
        # specific check, immediate and right before the one paid call this
        # function makes, so a lease already revoked by the reaper aborts
        # here rather than spending against a job someone else now owns.
        if job_id is not None and claimant is not None:
            if not jobs.assert_lease(job_id, claimant):
                raise RuntimeError(
                    f"lease lost for job {job_id} (claimant {claimant}) before the "
                    "analysis spend -- aborting without calling Claude"
                )
        # Set right before the analyze() call, whose spend guard is its first
        # line -- so a failure past this point (guard passed, stream/parse
        # failed) counts as an attempted Claude call, while a fetch_images
        # failure above never reaches here and correctly counts as zero.
        claude_attempted = True
        result = analyze.analyze(brand, sample, images, scraped_count=len(raw))
        # Second lease fence (P1-1/P1-2 fix), immediately AFTER the spend and
        # BEFORE the write it pays for: the paid call already happened --
        # unavoidable, nothing to do about that now -- but if the lease was
        # lost WHILE it ran, this result is now stale (the reaper already
        # re-queued the job for a new claimant, whose own re-run is the
        # authoritative one). Discard rather than save over it.
        if job_id is not None and claimant is not None:
            if not jobs.assert_lease(job_id, claimant):
                raise RuntimeError(
                    "lease lost during analysis — discarding result; another "
                    "claimant now owns this job"
                )
        store.save_analysis(comp_id, snap_id, result, meta, status="ok")
        status, est_calls = "done", 1
        proposals = len(result.get("proposed_research", []))
        print(
            f"saved analysis for {brand}: {len(sample)} of {len(raw)} ads analyzed, "
            f"{len(images)} images, {proposals} research proposals"
        )
    except ResearchSpendCapExceeded as exc:
        # A blocked run is not a failed analysis -- do not write a spurious
        # status="failed" row (the snapshot is already saved above). Report
        # clearly and exit non-zero without calling Claude. The guard blocks
        # BEFORE any Claude call, so this is always est_claude_calls=0.
        #
        # `exc.window` is "hour"/"day" for a real cap hit (count/limit are
        # meaningful) or "lock"/"persist"/"corrupt" for a fail-closed guard-
        # infrastructure failure (count/limit are placeholders); for the
        # latter, str(exc) already carries the specific reason -- e.g. for
        # "corrupt" it names the unreadable state file and how to reset it
        # -- so print it directly instead of the count/limit template.
        status, est_calls, err = "capped", 0, str(exc)
        if exc.window in ("hour", "day"):
            print(
                f"spend cap hit ({exc.count}/{exc.limit} this {exc.window}) — not calling Claude "
                f"for {brand}",
                file=sys.stderr,
            )
        else:
            print(f"{exc} — not calling Claude for {brand}", file=sys.stderr)
        sys.exit(1)  # `finally` below still runs before this propagates.
    except Exception as exc:  # noqa: BLE001 — always record an observable row (Codex P2-1)
        status, err = "failed", str(exc)
        est_calls = 1 if claude_attempted else 0
        # Best-effort observability write -- if this itself raises, the
        # `finally` below still fires and records the run as finished before
        # the new exception propagates (finish_run must never depend on this
        # succeeding; that was P1-1).
        #
        # Lease-gate this observability write too (P1 fix, Sol re-gate): the
        # post-analysis fence above raises RuntimeError on lease loss, and that
        # RuntimeError lands HERE. A revoked worker must write NOTHING to
        # research_analyses -- not even this empty status='failed' row -- because
        # it could still land after the new claimant's real analysis and surface
        # as the latest. So skip the write when we no longer hold the lease
        # (job context only; the direct/legacy call with no job_id always writes,
        # exactly as before).
        if job_id is None or claimant is None or jobs.assert_lease(job_id, claimant):
            store.save_analysis(comp_id, snap_id, {}, meta, status="failed", error=str(exc))
        print(f"analysis FAILED for {brand} (snapshot saved): {exc}", file=sys.stderr)
        traceback.print_exc()
        raise
    finally:
        # Guaranteed, exactly-once, best-effort: runs on every exit path above
        # (early return, sys.exit in the capped branch, or the re-raise here)
        # regardless of whether save_analysis succeeded.
        store.finish_run(run_id, status, scraped_ads=scraped, analyzed=analyzed, est_claude_calls=est_calls, error=err)
        # Fleet usage: flush whatever analyze.usage buffered this run (a
        # no-op if the reporter is in no-op mode or nothing was buffered).
        # UsageReporter.flush() never raises -- see its module docstring --
        # so this cannot mask the run's actual outcome/exception above.
        analyze.usage.flush()


def handle_scrape(job: dict, claimant: str) -> tuple:
    """Ads-as-scrape handler dispatched by `worker.jobs`'s `_dispatch` for
    (job_kind='scrape', connector='ad_library.scrapecreators') jobs (Task
    1.6). Reuses `run_ads()` -- the extracted, otherwise-UNCHANGED body of
    the former single-shot `main()` -- exactly as it already runs today.

    Decision A (do not double-guard/double-report): `analyze.analyze()`
    already calls `spend_guard.guard()` as its very first line (Layer 1)
    and already reports token usage via `analyze.usage`. This handler does
    NOT call `worker.budget.reserve()`/`settle()` for this path. Those exist
    for the FUTURE project-scoped collect/verify/synthesize handlers (Tasks
    P3-P4); calling them here, for this ad-hoc (`project_id=None`) job,
    would run `spend_guard.guard()` a SECOND time for the same Anthropic
    call and double-count spend -- exactly what the ad-hoc boundary (R3)
    exists to prevent. The job envelope this handler adds is lifecycle
    only: the lease (via `run_ads`'s own `assert_lease` call) and the
    (status, cost_cents) this function hands back to `drain()` for
    `finish_job`.

    `cost_cents` is always None here. `analyze.usage` (a `UsageReporter`)
    only ever reports RAW TOKEN COUNTS to the fleet usage-ingest endpoint,
    which prices them server-side (see `usage_reporter.py`'s own module
    docstring: "the server prices tokens ... this module carries NO
    pricing table and never computes cost from tokens itself") -- there is
    no client-side accrued-cents total this handler could read back after
    the call. Inventing a second, client-computed price here (e.g. from
    `settings.price_for`) would itself BE the extra spend path this task
    forbids: a price card is a worst-case RESERVATION ceiling, not the
    actual cost, and stamping it onto `cost_cents` would misrepresent an
    estimate as a fact.
    TODO(P2+): if `research_jobs.cost_cents` needs a real number (not None)
    for ad-hoc scrape jobs before the fleet usage-ingest endpoint exposes a
    priced-total-per-run lookup back to the caller, that is a new, explicit
    read path to design -- not a client-side re-derivation bolted on here.

    Never raises, and never lets `run_ads()`'s own `sys.exit(1)` (the
    spend-cap path) or its own `except Exception: ... raise` (any other
    failure) escape -- one job's failure must fail only that job, never
    crash the shared `drain()`/`run_one()` loop or the worker process.
    `run_ads()` itself is untouched otherwise: it still prints its existing
    messages and still writes its own `research_runs` row via
    start_run/finish_run exactly as the old `main()` did. This wrapper
    decides what `_dispatch()`/`finish_job` gets next: `('done', None,
    None)` on a clean return (including the no-op `no_ads` path -- a
    successful scrape that simply found nothing to analyze is not a
    failure), or `('failed', None, error)` if `run_ads()` exited via the
    spend cap's `sys.exit(1)` or raised any other exception -- `error` is a
    real, non-None message in both failure cases (P2-2 fix: previously
    always None here, silently dropping the actual reason on the floor
    instead of letting `finish_job` record it).
    """
    params = job.get("params") or {}
    brand = params.get("brand")
    domain = params.get("domain")
    job_id = job.get("id")

    try:
        run_ads(brand, domain, job_id=job_id, claimant=claimant)
    except SystemExit as exc:
        logger.warning(
            "scrape job %s (brand=%s) hit the spend cap (exit code %s)",
            job_id, brand, exc.code,
        )
        return "failed", None, f"spend cap hit (exit code {exc.code})"
    except Exception as exc:  # noqa: BLE001 -- never let this handler crash the drain loop
        logger.warning("scrape job %s (brand=%s) failed: %s", job_id, brand, exc)
        return "failed", None, str(exc)
    return "done", None, None


def _normalize_for_idem(brand: str) -> str:
    """A stable, key-safe component for the idempotency key's competitor
    slot -- lowercased, internal whitespace collapsed to single hyphens.
    NOT a competitor id (the job row's own `competitor_id` column already
    carries that from `get_or_create_competitor`); this just keeps two
    differently-cased/spaced invocations of the SAME brand name
    ("MeUndies" vs " meundies ") mapping to the SAME idempotency key, while
    staying human-readable for debugging."""
    return "-".join(brand.strip().lower().split())


def main(brand: str, domain: Optional[str] = None) -> None:
    """CLI entrypoint (Task 1.7, rewritten by the P1-4/P1-5 fix): enqueue one
    `scrape`/`ad_library.scrapecreators` `research_jobs` row for this
    brand/day, reap any stale claim, then run EXACTLY that one job via
    `jobs.run_one()` -- never the whole queue (the old `drain(...,
    once=True)` call would also claim and fail every other queued kind this
    CLI has no handler for yet: collect/verify/synthesize). The ads
    orchestration itself runs identically to before (see
    `run_ads`/`handle_scrape`) -- this function is pure job-spine plumbing,
    not the orchestration itself.

    The idempotency key is `jobs.idem_scrape_ads(jobs.ADHOC_PROJECT,
    _normalize_for_idem(brand), <today's yyyymmdd>)` -- re-running
    `python -m worker.run "<brand>"` again later THE SAME DAY is therefore a
    silent enqueue no-op (Task 1.1/R4: `jobs.enqueue()` returns None).

    Target selection, then an AUTHORITATIVE terminal status:
      - Fresh enqueue (`job_id` is not None): this IS the job this
        invocation just created; run it via `jobs.run_one(job_id)`.
      - Same-day duplicate (`job_id` is None): `store.find_job_by_idem`
        looks up the existing row. If it's still 'queued' (e.g. an earlier
        invocation died before even claiming), finish it off via
        `jobs.run_one(existing['id'])`. If it's already TERMINAL ('done' or
        'failed'), that status IS the answer -- no need to run anything again.
        A 'claimed'/'running' row is owned by another worker right now and may
        still end 'failed', so it is NOT authoritative here -- it falls through
        to the indeterminate (None) path, exactly like a row that can't be
        found/read at all (a lost best-effort read).

    `jobs.reap_stale()` runs between the enqueue and target selection so a
    stale claim left behind by an earlier, killed invocation of today's
    job (e.g. a Cloud Run Job that was killed mid-run) is freed before this
    invocation tries to run it -- otherwise `run_one` could find it still
    claimed/running and return None instead of actually running it.

    Exit code, fail-CLOSED on the AUTHORITATIVE status (P1-5 fix: no longer
    a fail-open best-effort re-read after a whole-queue drain -- `run_one`/
    `_run_claimed` return exactly what `finish_job` was called with for
    THIS job): `final == 'failed'` exits 1. `final is None` -- a genuine
    "no authoritative outcome" edge (couldn't claim; another worker now owns
    the row; the duplicate-path read failed) -- logs a warning and exits 0
    rather than assuming failure; the run's real outcome is independently
    recorded in `research_runs` regardless. Any other status (done,
    no_ads via 'done', capped, or an existing terminal status read
    directly) exits 0.

    `project_id` is omitted (ad-hoc, R3) -- this is the pre-existing,
    already-legally-enabled ads path, not a scoped research project.
    `competitor_id` is resolved up front via the same `get_or_create_
    competitor` call `run_ads()` itself will ALSO make once dispatched -- a
    small, accepted redundancy (idempotent either way) so the job row can
    carry a real `competitor_id` without any change to `run_ads()`'s own,
    otherwise-unchanged internals.
    """
    comp_id = store.get_or_create_competitor(brand, domain)
    yyyymmdd = datetime.now().strftime("%Y%m%d")
    idem_key = jobs.idem_scrape_ads(jobs.ADHOC_PROJECT, _normalize_for_idem(brand), yyyymmdd)
    job_id = jobs.enqueue(
        "scrape",
        {"brand": brand, "domain": domain, "connector": "ad_library.scrapecreators"},
        idem_key,
        project_id=None,
        competitor_id=comp_id,
    )

    # Free up any stale claim on today's job left behind by an earlier,
    # killed invocation before this one tries to run it.
    jobs.reap_stale()

    if job_id is not None:
        # Fresh enqueue -- this IS the job this invocation just created;
        # run exactly it, never the whole queue (P1-4).
        final = jobs.run_one(job_id)
    else:
        # Same-day duplicate (Task 1.1/R4): jobs.enqueue silently no-op'd.
        existing = store.find_job_by_idem(idem_key)
        if existing is not None and existing.get("status") == "queued":
            final = jobs.run_one(existing["id"])
        elif existing is not None and existing.get("status") in ("done", "failed"):
            # Only a TERMINAL status is authoritative here. A 'claimed'/'running'
            # row is owned by another worker right now and may still end up
            # 'failed' -- treating it as a successful terminal outcome (exit 0)
            # would hide that. Fall through to the indeterminate (None) path.
            final = existing.get("status")
        else:
            final = None

    if final == "failed":
        sys.exit(1)
    if final is None:
        logger.warning(
            "no authoritative terminal status for brand=%s (idem_key=%s) -- "
            "exiting 0; the run's real outcome is recorded in research_runs",
            brand, idem_key,
        )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m worker.run <brand> [domain]", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
