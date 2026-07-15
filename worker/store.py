"""Supabase persistence for the Research Studio worker.

Service-role only (the tables are RLS deny-all to `authenticated`). Uses the new
sb_secret_ service key — legacy anon/service_role keys are disabled on this project.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from supabase import create_client, Client

from worker.config import settings

logger = logging.getLogger(__name__)


def _client() -> Client:
    return create_client(settings.supabase_url, settings.supabase_service_key)


def get_or_create_competitor(name: str, domain: Optional[str] = None) -> str:
    """Return the competitor id for `name`, creating the row if absent.
    Reuses the shared `competitors` identity (gmail_label is now nullable)."""
    sb = _client()
    existing = sb.table("competitors").select("id").eq("name", name).limit(1).execute()
    if existing.data:
        return existing.data[0]["id"]
    created = sb.table("competitors").insert({"name": name, "domain": domain}).execute()
    return created.data[0]["id"]


def save_snapshot(competitor_id: str, platform_id: str, ads: list) -> str:
    """Insert one timestamped ad snapshot (never overwritten). Returns its id."""
    sb = _client()
    row = sb.table("research_ad_snapshots").insert({
        "competitor_id": competitor_id,
        "platform_id": platform_id,
        "ads": ads,
        "ad_count": len(ads),
    }).execute()
    return row.data[0]["id"]


def save_analysis(
    competitor_id: str,
    snapshot_id: Optional[str],
    result: dict,
    meta: dict,
    status: str = "ok",
    error: Optional[str] = None,
) -> str:
    """Insert one analysis row. Always called — on failure, pass status='failed'
    + error so the row is observable rather than an orphan snapshot (Codex P2-1).

    `proposed_research` is written when the column exists; if the migration adding
    it hasn't been applied yet, the insert gracefully degrades (retry without it +
    warn) so the worker never breaks on a not-yet-migrated databank.
    """
    sb = _client()
    row = {
        "competitor_id": competitor_id,
        "snapshot_id": snapshot_id,
        "playbook": result.get("playbook", {}),
        "winning": result.get("winning", []),
        "proposed_research": result.get("proposed_research", []),
        "model": meta.get("model", ""),
        "distinct_ads": meta.get("distinct_ads", 0),
        "images_analyzed": meta.get("images_analyzed", 0),
        "status": status,
        "error": error,
    }
    try:
        res = sb.table("research_analyses").insert(row).execute()
    except Exception as exc:  # noqa: BLE001
        if "proposed_research" in str(exc).lower():
            logger.warning("proposed_research column missing — run migration 025; storing without it")
            row.pop("proposed_research", None)
            res = sb.table("research_analyses").insert(row).execute()
        else:
            raise
    return res.data[0]["id"]


def start_run(brand: str, competitor_id: Optional[str] = None) -> Optional[str]:
    """Record that a worker run has STARTED (migration 038: research_runs), so it's
    observable while it's in flight rather than only once `save_analysis` lands at
    the end. Best-effort: this is observability, not the analysis itself, so ANY
    failure here (table not yet migrated, network blip, ...) must never break or
    block the worker — log a one-line warning and return None. Callers must treat a
    None run_id as "registry unavailable" and no-op the corresponding finish_run."""
    try:
        sb = _client()
        row = sb.table("research_runs").insert({
            "brand": brand,
            "competitor_id": competitor_id,
            "status": "running",
        }).execute()
        return row.data[0]["id"]
    except Exception as exc:  # noqa: BLE001 — best-effort, must never raise
        logger.warning("start_run: could not record run start for %s (%s)", brand, exc)
        return None


def finish_run(
    run_id: Optional[str],
    status: str,
    scraped_ads: Optional[int] = None,
    analyzed: Optional[int] = None,
    est_claude_calls: int = 0,
    error: Optional[str] = None,
) -> None:
    """Record that a worker run FINISHED (migration 038: research_runs). No-op if
    `run_id` is None (start_run already failed/degraded for this run). Best-effort,
    same as start_run: any failure here must never raise."""
    if run_id is None:
        return
    try:
        sb = _client()
        sb.table("research_runs").update({
            "status": status,
            # PostgREST casts JSON values to the column type via Postgres's own input
            # parser, which accepts the bare special value 'now' but NOT the function-
            # call syntax 'now()' as a timestamptz literal -- so this is stamped
            # client-side rather than sent as a SQL-function string.
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "scraped_ads": scraped_ads,
            "analyzed": analyzed,
            "est_claude_calls": est_claude_calls,
            "error": error,
        }).eq("id", run_id).execute()
    except Exception as exc:  # noqa: BLE001 — best-effort, must never raise
        logger.warning("finish_run: could not record run finish for run_id=%s (%s)", run_id, exc)
