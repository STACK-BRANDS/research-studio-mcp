"""Supabase persistence for the Research Studio worker.

Service-role only (the tables are RLS deny-all to `authenticated`). Uses the new
sb_secret_ service key — legacy anon/service_role keys are disabled on this project.
"""
from typing import Optional

from supabase import create_client, Client

from worker.config import settings


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
    + error so the row is observable rather than an orphan snapshot (Codex P2-1)."""
    sb = _client()
    row = sb.table("research_analyses").insert({
        "competitor_id": competitor_id,
        "snapshot_id": snapshot_id,
        "playbook": result.get("playbook", {}),
        "winning": result.get("winning", []),
        "model": meta.get("model", ""),
        "distinct_ads": meta.get("distinct_ads", 0),
        "images_analyzed": meta.get("images_analyzed", 0),
        "status": status,
        "error": error,
    }).execute()
    return row.data[0]["id"]
