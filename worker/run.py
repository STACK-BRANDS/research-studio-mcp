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


def main(brand: str, domain: Optional[str] = None) -> None:
    comp_id = store.get_or_create_competitor(brand, domain)
    platform_id = ingest.resolve_platform_id(brand)
    ads = ingest.dedup(ingest.pull_ads(platform_id))
    snap_id = store.save_snapshot(comp_id, platform_id, ads)

    meta = {"model": settings.model, "distinct_ads": len(ads), "images_analyzed": 0}
    try:
        images = ingest.fetch_images(ads, cap=settings.max_images)
        meta["images_analyzed"] = len(images)
        result = analyze.analyze(brand, ads, images)
        store.save_analysis(comp_id, snap_id, result, meta, status="ok")
        print(f"saved analysis for {brand}: {len(ads)} ads, {len(images)} images")
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
