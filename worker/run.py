"""Research Studio worker entrypoint.

    python -m worker.run "MeUndies" [domain]

Scrapes a competitor's live Meta ads, analyzes them server-side with Claude
(vision + structured output), and persists a snapshot + analysis to Supabase.
Requires the server-side env (see .env.example): ANTHROPIC_API_KEY,
SCRAPECREATORS_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY.
"""
import sys
import traceback
from typing import Optional

from worker import ingest, analyze, store
from worker.config import settings
from worker.spend_guard import ResearchSpendCapExceeded


def main(brand: str, domain: Optional[str] = None) -> None:
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
        platform_id = ingest.resolve_platform_id(brand)
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
        # Set right before the analyze() call, whose spend guard is its first
        # line -- so a failure past this point (guard passed, stream/parse
        # failed) counts as an attempted Claude call, while a fetch_images
        # failure above never reaches here and correctly counts as zero.
        claude_attempted = True
        result = analyze.analyze(brand, sample, images, scraped_count=len(raw))
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
        store.save_analysis(comp_id, snap_id, {}, meta, status="failed", error=str(exc))
        print(f"analysis FAILED for {brand} (snapshot saved): {exc}", file=sys.stderr)
        traceback.print_exc()
        raise
    finally:
        # Guaranteed, exactly-once, best-effort: runs on every exit path above
        # (early return, sys.exit in the capped branch, or the re-raise here)
        # regardless of whether save_analysis succeeded.
        store.finish_run(run_id, status, scraped_ads=scraped, analyzed=analyzed, est_claude_calls=est_calls, error=err)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m worker.run <brand> [domain]", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
