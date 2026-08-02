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


def get_pinned_platform_id(competitor_id: str) -> Optional[str]:
    """Return a previously-recorded platform_id for this competitor from
    `research_page_identities`, if one exists -- so the worker can skip the
    error-prone fuzzy search entirely once a page has been pinned or observed
    before. Prefers a row with `kind='primary'` (an explicit operator pin);
    falls back to the most-recently-seen row of any kind (e.g. an earlier
    'observed' resolution) when no primary exists. Returns None if this
    competitor has no recorded identity yet.

    Best-effort, same contract as start_run/finish_run: this is a READ used to
    short-circuit resolve_platform_id, not the analysis itself -- ANY failure
    here (table not yet migrated, network blip, ...) must never break or block
    the worker. On failure, log a warning and return None so the caller falls
    back to the existing fuzzy-resolve path.
    """
    try:
        sb = _client()
        res = (
            sb.table("research_page_identities")
            .select("platform_id, kind, first_seen")
            .eq("competitor_id", competitor_id)
            .order("first_seen", desc=True)
            .execute()
        )
        rows = res.data or []
        for row in rows:
            if row.get("kind") == "primary":
                return row["platform_id"]
        return rows[0]["platform_id"] if rows else None
    except Exception as exc:  # noqa: BLE001 — best-effort, must never raise
        logger.warning(
            "get_pinned_platform_id: could not read pinned identity for competitor_id=%s (%s)",
            competitor_id, exc,
        )
        return None


def record_page_identity(
    competitor_id: str,
    platform_id: str,
    page_name: Optional[str] = None,
    kind: str = "observed",
) -> None:
    """Record a competitor -> Meta platform_id resolution into
    `research_page_identities`, so a fuzzy resolution is captured for future
    runs (pin-first: get_pinned_platform_id will find it next time) and for
    later operator review/curation.

    Dedup: skips the insert if a row with this exact (competitor_id,
    platform_id) pair already exists, so re-running the same brand doesn't
    pile up duplicate rows every run.

    `kind='observed'` (the default) marks a fuzzy-resolved id -- this function
    never auto-promotes a resolution to `kind='primary'`; that stays an
    explicit, separate curation action. Pass kind='primary' only when the
    caller is deliberately pinning an id.

    Best-effort, same contract as start_run/finish_run: this is capture/
    observability, not the analysis itself -- ANY failure here (table not yet
    migrated, network blip, duplicate race, ...) must never break or block the
    worker.
    """
    try:
        sb = _client()
        existing = (
            sb.table("research_page_identities")
            .select("id")
            .eq("competitor_id", competitor_id)
            .eq("platform_id", platform_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            return
        sb.table("research_page_identities").insert({
            "competitor_id": competitor_id,
            "platform_id": platform_id,
            "page_name": page_name,
            "kind": kind,
        }).execute()
    except Exception as exc:  # noqa: BLE001 — best-effort, must never raise
        logger.warning(
            "record_page_identity: could not record identity for competitor_id=%s platform_id=%s (%s)",
            competitor_id, platform_id, exc,
        )


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
    analysis_id = res.data[0]["id"]
    # Mechanically derive research_angle_observations from this analysis's own
    # per-ad rows (v2 spec §4.3: "derived MECHANICALLY from playbook-v2 per-ad
    # rows at analysis-save time" -- pure code, no model, never a rollup a model
    # writes). Best-effort: save_angle_observations never raises, so a missing
    # table or bad row shape cannot fail this save.
    playbook = row.get("playbook") or {}
    per_ad = playbook.get("per_ad") or []
    save_angle_observations(analysis_id, competitor_id, per_ad)
    return analysis_id


def save_angle_observations(
    analysis_id: str,
    competitor_id: str,
    per_ad: list,
    store_id: Optional[str] = None,
) -> None:
    """Group a saved analysis's playbook.per_ad rows by angle_key and insert
    one `research_angle_observations` row per angle_key present (v2 spec
    §4.3). Pure aggregation over rows already produced by the single
    analyze() call -- no model, no extra Anthropic call.

    `ad_count` = rows sharing that angle_key. `top_longevity_days` = max
    `days_active` among them (null-safe: skips rows where it's missing/not
    an int). `verbatim_example` = {quote, ad_ref, source_url} from one
    representative ad in the group (the first encountered) -- `quote` is its
    `hook` (falling back to its free-text `angle`), `ad_ref` is its `ad_id`,
    `source_url` is None (per-ad rows carry no URL field today).

    `store_id` is passed through as-is (None today -- this worker doesn't
    yet track a per-store scope for competitor teardown analyses; the column
    is nullable for exactly this reason).

    Idempotent per analysis_id: deletes any existing observations for this
    analysis_id before inserting, so a re-run that reuses an analysis_id
    (shouldn't happen -- each run gets a fresh one -- but cheap to guard)
    doesn't double-count.

    Best-effort, same "never break the worker" contract as start_run/
    finish_run: ANY failure (table not yet migrated, network blip, angle_key
    not in the registry, ...) is logged and swallowed, never raised. A
    missing table or failed insert must not fail the analysis save.
    """
    try:
        groups: dict[str, list] = {}
        for ad_row in per_ad:
            if not isinstance(ad_row, dict):
                continue
            angle_key = ad_row.get("angle_key") or "unmapped"
            groups.setdefault(angle_key, []).append(ad_row)

        observations = []
        for angle_key, rows in groups.items():
            longevities = [
                r["days_active"] for r in rows
                if isinstance(r.get("days_active"), int) and not isinstance(r.get("days_active"), bool)
            ]
            top_longevity = max(longevities) if longevities else None
            example = rows[0]
            observations.append({
                "analysis_id": analysis_id,
                "competitor_id": competitor_id,
                "store_id": store_id,
                "angle_key": angle_key,
                "ad_count": len(rows),
                "top_longevity_days": top_longevity,
                "verbatim_example": {
                    "quote": example.get("hook") or example.get("angle") or "",
                    "ad_ref": example.get("ad_id"),
                    "source_url": None,
                },
            })

        sb = _client()
        # Idempotency: clear any pre-existing observations for this
        # analysis_id first (see docstring).
        # Delete-then-insert runs even when there are no observations (empty
        # per_ad): an empty re-save must still clear any stale rows, honouring
        # the per-analysis_id idempotency contract (Terra P1). Insert only when
        # there is something to write (an empty insert is a needless call).
        sb.table("research_angle_observations").delete().eq("analysis_id", analysis_id).execute()
        if observations:
            sb.table("research_angle_observations").insert(observations).execute()
    except Exception as exc:  # noqa: BLE001 — best-effort, must never raise
        logger.warning(
            "save_angle_observations: could not derive/save angle observations for analysis_id=%s (%s)",
            analysis_id, exc,
        )


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
