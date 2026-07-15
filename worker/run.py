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
    platform_id = ingest.resolve_platform_id(brand)
    # Pull wide + dedup; the SNAPSHOT keeps everything (spec §9). The expensive
    # analysis runs on a scope-aware sample (active-first, recency+longevity).
    raw = ingest.dedup(ingest.pull_ads(platform_id))
    snap_id = store.save_snapshot(comp_id, platform_id, raw)

    # Guard: a resolved page with no active ads (e.g. a brand page whose ads run
    # under a replacement/"II" or persona identity) shouldn't burn an Anthropic call
    # on an empty analysis. Record it observably and stop.
    if not raw:
        store.save_analysis(
            comp_id, snap_id, {},
            {"model": settings.model, "distinct_ads": 0, "images_analyzed": 0, "scraped_ads": 0},
            status="no_ads",
            error="Resolved page returned no active ads — try the exact ad-running page name.",
        )
        print(f"no ads for {brand}: resolved page has no active ads — try the exact ad-running page name", file=sys.stderr)
        return

    sample = ingest.select_for_analysis(raw)

    meta = {
        "model": settings.model,
        "distinct_ads": len(sample),
        "images_analyzed": 0,
        "scraped_ads": len(raw),
    }
    try:
        images = ingest.fetch_images(sample, cap=settings.max_images)
        meta["images_analyzed"] = len(images)
        result = analyze.analyze(brand, sample, images, scraped_count=len(raw))
        store.save_analysis(comp_id, snap_id, result, meta, status="ok")
        proposals = len(result.get("proposed_research", []))
        print(
            f"saved analysis for {brand}: {len(sample)} of {len(raw)} ads analyzed, "
            f"{len(images)} images, {proposals} research proposals"
        )
    except ResearchSpendCapExceeded as exc:
        # A blocked run is not a failed analysis -- do not write a spurious
        # status="failed" row (the snapshot is already saved above). Report
        # clearly and exit non-zero without calling Claude.
        #
        # `exc.window` is "hour"/"day" for a real cap hit (count/limit are
        # meaningful) or "lock"/"persist"/"corrupt" for a fail-closed guard-
        # infrastructure failure (count/limit are placeholders); for the
        # latter, str(exc) already carries the specific reason -- e.g. for
        # "corrupt" it names the unreadable state file and how to reset it
        # -- so print it directly instead of the count/limit template.
        if exc.window in ("hour", "day"):
            print(
                f"spend cap hit ({exc.count}/{exc.limit} this {exc.window}) — not calling Claude "
                f"for {brand}",
                file=sys.stderr,
            )
        else:
            print(f"{exc} — not calling Claude for {brand}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 — always record an observable row (Codex P2-1)
        store.save_analysis(comp_id, snap_id, {}, meta, status="failed", error=str(exc))
        print(f"analysis FAILED for {brand} (snapshot saved): {exc}", file=sys.stderr)
        traceback.print_exc()
        raise


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m worker.run <brand> [domain]", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
