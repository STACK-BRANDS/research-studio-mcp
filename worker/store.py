"""Supabase persistence for the Research Studio worker.

Service-role only (the tables are RLS deny-all to `authenticated`). Uses the new
sb_secret_ service key — legacy anon/service_role keys are disabled on this project.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from supabase import create_client, Client

from worker.config import settings

logger = logging.getLogger(__name__)

# The single Supabase Storage bucket every site-capture connector writes to
# (Phase 2, migrations 142-144). A literal constant, like every table name
# elsewhere in this module -- not a `settings` tunable, since the deployed
# `rs_create_site_capture` contract already fixes `captured_html_path` to a
# `captures/<sha>.*` layout within this specific bucket.
RESEARCH_MEDIA_BUCKET = "research-media"


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


# ---------------------------------------------------------------------------
# Job spine (research_jobs) -- CAS claim/lease/heartbeat + stale-claim reaper.
# Phase 1 tasks 1.2/1.3 of the deep-research plan (architecture §13 R1).
#
# Unlike the observability writes above, NONE of these are best-effort: a
# claim/lease/finish mutation is a job-spine primitive whose caller (worker.
# jobs) depends on an honest True/False (or raised exception) to decide
# whether it still owns the row. Swallowing an exception here would hide a
# real DB failure from a caller that needs to know about it to back off
# safely -- so exceptions propagate; only "0 rows matched" collapses to a
# clean False.
# ---------------------------------------------------------------------------

def claim_job(job_kind: Optional[str], claimant: str) -> Optional[dict]:
    """Call the deployed `rs_claim_job` RPC: it does `FOR UPDATE SKIP LOCKED`
    on the oldest queued row (optionally filtered to `job_kind`), stamps
    status='claimed', claimed_by=claimant, claimed_at=now(), and returns the
    claimed row -- or nothing if no row was queued for this kind. The claim
    (and the SKIP LOCKED concurrency guarantee) is entirely the database's;
    this function never reimplements it in Python.

    `job_kind=None` omits the `p_job_kind` param entirely (rather than
    sending an explicit null), so the RPC's own default (any kind, oldest
    first) applies.
    """
    sb = _client()
    params = {"p_claimed_by": claimant}
    if job_kind is not None:
        params["p_job_kind"] = job_kind
    res = sb.rpc("rs_claim_job", params).execute()
    data = res.data
    if not data:
        return None
    return data[0] if isinstance(data, list) else data


def claim_job_by_id(job_id: str, claimant: str) -> Optional[dict]:
    """CAS research_jobs: queued -> claimed for one SPECIFIC job_id, rather
    than the next-oldest-queued-of-any-kind row `claim_job`/`rs_claim_job`
    picks. This is the CLI's "claim the exact job I just enqueued (or the
    same-day duplicate I'm re-attempting), never the whole queue" path
    (Task 1.7, P1-4/P1-5 fix) -- `worker.jobs.run_one` is the only caller.
    Stamps `claimed_by=claimant`, `claimed_at=now()`. Returns the claimed
    row, or None if `job_id` was no longer 'queued' by the time this ran
    (already claimed/running by something else, or already terminal) -- the
    caller must not assume it now owns the job.

    NOT best-effort (see the job-spine banner above): this is a job-spine
    CAS primitive whose caller depends on an honest row/None to decide
    whether it claimed the job; only "0 rows matched" collapses to a clean
    None, any other failure (network, DB) propagates unchanged.
    """
    sb = _client()
    res = (
        sb.table("research_jobs")
        .update({
            "status": "claimed",
            "claimed_by": claimant,
            "claimed_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("id", job_id)
        .eq("status", "queued")
        .execute()
    )
    data = res.data
    if not data:
        return None
    return data[0] if isinstance(data, list) else data


def mark_job_running(job_id: str, claimant: str) -> bool:
    """CAS research_jobs: claimed -> running, stamping started_at. True iff a
    row matched (this claimant still held the claim); False means the lease
    was already revoked (0 rows affected) and the caller must abort without
    running the handler at all."""
    sb = _client()
    res = (
        sb.table("research_jobs")
        .update({"status": "running", "started_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", job_id)
        .eq("claimed_by", claimant)
        .eq("status", "claimed")
        .execute()
    )
    return bool(res.data)


def heartbeat_job(job_id: str, claimant: str) -> bool:
    """CAS research_jobs: bump claimed_at while status stays 'running', so a
    live, still-working claimant's lease never goes stale under the reaper's
    timeout. True iff this claimant still holds the running lease; False
    means it was already revoked -- the caller must stop and abort."""
    sb = _client()
    res = (
        sb.table("research_jobs")
        .update({"claimed_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", job_id)
        .eq("claimed_by", claimant)
        .eq("status", "running")
        .execute()
    )
    return bool(res.data)


def job_lease_held(job_id: str, claimant: str) -> bool:
    """Read-only fence check: True iff `claimant` currently holds a 'running'
    lease on `job_id`. Callers must check this immediately before any
    external write or spend inside a handler and abort doing nothing on
    False."""
    sb = _client()
    res = (
        sb.table("research_jobs")
        .select("id")
        .eq("id", job_id)
        .eq("claimed_by", claimant)
        .eq("status", "running")
        .limit(1)
        .execute()
    )
    return bool(res.data)


def finish_job(
    job_id: str,
    claimant: str,
    status: str,
    error: Optional[str] = None,
    cost_cents: Optional[int] = None,
    result: Optional[dict] = None,
) -> bool:
    """CAS research_jobs: running -> done|failed (terminal), stamping
    finished_at plus any of error/cost_cents/result that were passed (a
    field left as None is omitted from the update, never written over an
    existing value with a bare null). True iff this claimant still held the
    running lease at the moment of finishing; False means the lease was
    revoked mid-run (the reaper already re-queued the job as a fresh
    attempt) and the caller's result must be discarded, not written."""
    sb = _client()
    update: dict = {"status": status, "finished_at": datetime.now(timezone.utc).isoformat()}
    if error is not None:
        update["error"] = error
    if cost_cents is not None:
        update["cost_cents"] = cost_cents
    if result is not None:
        update["result"] = result
    res = (
        sb.table("research_jobs")
        .update(update)
        .eq("id", job_id)
        .eq("claimed_by", claimant)
        .eq("status", "running")
        .execute()
    )
    return bool(res.data)


def rs_reserve_spend(
    job_id: str,
    project_id: str,
    ref: str,
    est_cents: int,
    claimant: str,
    connector: Optional[str] = None,
) -> str:
    """Call the deployed `rs_reserve_spend` RPC (migration 138): a
    project-scoped, worst-case spend reservation over `research_budget_ledger`,
    guarded server-side by the job's lease (`claimed_by=claimant AND
    status='running'`), the project's binding/approval state, and its budget
    ceiling. `p_est_cents` MUST be the worst-case ceiling for the call (see
    `worker.config.Settings.price_cards`) -- the RPC enforces that the
    project's committed spend (actual + outstanding reservations) plus this
    reservation never exceeds `research_projects.budget_cents`.

    Returns the RPC's own scalar text result: `'ok'` (a fresh reserve was
    recorded -- the caller MAY make the paid call) or `'skip'` (a reserve for
    this exact `ref` already exists -- a replay; the caller must NOT call the
    provider again).

    NOT best-effort (see the job-spine banner above): any guard failure
    inside the RPC -- lease not held, job not bound to `project_id`, project
    not approved, ceiling would be exceeded -- RAISES and propagates here
    unchanged. `worker.budget.reserve()` must not swallow this; a caller that
    cannot tell "blocked" from "allowed" could make an unreserved paid call.

    `connector` is omitted from the RPC params entirely when None (rather
    than sent as an explicit null), matching `claim_job`'s convention for
    optional RPC params.
    """
    sb = _client()
    params: dict = {
        "p_job_id": job_id,
        "p_project_id": project_id,
        "p_ref": ref,
        "p_est_cents": est_cents,
        "p_claimant": claimant,
    }
    if connector is not None:
        params["p_connector"] = connector
    res = sb.rpc("rs_reserve_spend", params).execute()
    return res.data


def rs_settle_call(ref: str, actual_cents: int, claimant: Optional[str] = None) -> None:
    """Call the deployed `rs_settle_call` RPC (migration 138): records the
    TRUE actual cost for `ref` in `research_budget_ledger` and releases its
    reservation hold. Idempotent server-side -- calling it twice for the same
    `ref` is a no-op. Requires a matching `rs_reserve_spend` reserve to
    already exist for `ref`.

    NOT best-effort (see the job-spine banner above): any RPC failure (no
    matching reserve, RPC/DB error) propagates unchanged -- a settle call
    that silently swallowed a failure would leave the ledger's reservation
    hold in place forever, understating the project's available budget.

    `claimant` is omitted from the RPC params entirely when None, matching
    `rs_reserve_spend`'s convention for optional RPC params.
    """
    sb = _client()
    params: dict = {"p_ref": ref, "p_actual_cents": actual_cents}
    if claimant is not None:
        params["p_claimant"] = claimant
    sb.rpc("rs_settle_call", params).execute()


def reap_stale_jobs(job_kind: str, timeout_seconds: int) -> int:
    """Re-queue every `job_kind` row stuck in claimed/running whose
    claimed_at is older than `timeout_seconds` (a dead worker: it stopped
    heartbeating). A single server-side UPDATE -- no read-then-write race.
    A live worker calling heartbeat_job() keeps claimed_at fresh and is never
    matched by this filter. Returns the number of rows re-queued."""
    sb = _client()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
    # Deliberately NOT fenced on claimed_by (P1-6, reviewed and rejected as a
    # fix): the reaper's whole job is to reclaim ANY dead worker's stale
    # claim, and there is no single claimant to CAS against here -- it must
    # match every claimant whose claimed_at has gone cold, not one specific
    # one. A live worker's own heartbeat keeps its claimed_at fresh, so it is
    # never matched by the `lt(claimed_at, cutoff)` filter regardless of
    # whose claimed_by is on the row.
    res = (
        sb.table("research_jobs")
        .update({"status": "queued", "claimed_by": None})
        .in_("status", ["claimed", "running"])
        .eq("job_kind", job_kind)
        .lt("claimed_at", cutoff)
        .execute()
    )
    return len(res.data or [])


def enqueue_job(
    job_kind: str,
    params: dict,
    idempotency_key: str,
    project_id: Optional[str] = None,
    competitor_id: Optional[str] = None,
    store_id: Optional[str] = None,
) -> Optional[str]:
    """Insert a new `research_jobs` row (Task 1.7). `status` is omitted from
    the payload entirely -- the column's own DB default ('queued') applies,
    same convention as every optional field below.

    Duplicate enqueue (an `idempotency_key` that already exists -- e.g. a
    retried CLI invocation for the same brand/day, Task 1.1's R4 keys) is a
    SILENT NO-OP: returns None rather than raising or returning the existing
    row's id, so a caller only ever gets an id back for a job it actually
    just caused to be inserted.

    Implemented as `upsert(..., on_conflict="idempotency_key",
    ignore_duplicates=True)` -- i.e. PostgREST's own translation of that
    flag combination is `INSERT ... ON CONFLICT (idempotency_key) DO
    NOTHING` -- rather than a plain `insert()` wrapped in a caught 23505.
    Chosen because DO NOTHING is race-free against a second, concurrent
    enqueuer for the exact same key (the conflict is resolved entirely
    server-side, in the same statement, with no separate error path to
    parse) and because a conflicting row is simply never returned in
    `.data` -- PostgREST never touched it -- so an EMPTY `.data` is itself
    the unambiguous duplicate signal, with no need to inspect an exception's
    error code at all.

    `project_id`/`competitor_id`/`store_id` are omitted from the insert
    payload entirely when None (matching `claim_job`/`rs_reserve_spend`'s
    existing convention for optional columns/params elsewhere in this
    module), rather than sent as explicit JSON nulls -- both are equivalent
    for a nullable column, but omitting keeps the payload minimal and
    consistent with the rest of this file.
    """
    sb = _client()
    row: dict = {
        "job_kind": job_kind,
        "params": params,
        "idempotency_key": idempotency_key,
    }
    if project_id is not None:
        row["project_id"] = project_id
    if competitor_id is not None:
        row["competitor_id"] = competitor_id
    if store_id is not None:
        row["store_id"] = store_id
    res = (
        sb.table("research_jobs")
        .upsert(row, on_conflict="idempotency_key", ignore_duplicates=True)
        .execute()
    )
    if not res.data:
        return None
    return res.data[0]["id"]


def job_status_for_idem(idempotency_key: str) -> Optional[str]:
    """Read a job's current status by its idempotency_key.

    Kept as a general-purpose status-only read (P1-4/P1-5 fix note: the CLI
    `worker.run.main` no longer calls this after a whole-queue `drain()` --
    it now calls `jobs.run_one()` directly for an authoritative terminal
    status, and uses `find_job_by_idem` below, not this function, on the
    same-day-duplicate path where it needs BOTH the id and the status). This
    stays available as the narrower status-only read for any other caller
    that only needs the status string.

    Unlike the CAS mutations above (which propagate exceptions because their
    caller depends on an honest True/False), this is a READ that only feeds
    a best-effort SIGNAL, and a job's real outcome is independently recorded
    in `research_jobs` itself (and, for the ads path, `research_runs` via
    `finish_run`). So it is deliberately best-effort: returns the status
    string, or None if the row is absent or the read failed -- a lost read
    here must never itself crash a caller or mask an already-persisted
    outcome."""
    try:
        sb = _client()
        res = (
            sb.table("research_jobs")
            .select("status")
            .eq("idempotency_key", idempotency_key)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0].get("status")
        return None
    except Exception as exc:  # noqa: BLE001 — best-effort exit-code signal only
        logger.warning("job_status_for_idem: could not read status for %s (%s)", idempotency_key, exc)
        return None


def find_job_by_idem(idempotency_key: str) -> Optional[dict]:
    """Read `{"id": ..., "status": ...}` for the research_jobs row with this
    idempotency_key, or None if absent or the read failed.

    Used by `worker.run.main` on a same-day duplicate enqueue (Task 1.1/R4:
    `jobs.enqueue` returned None because the row already exists) to find
    that EXISTING job's id -- so it can call `jobs.run_one` on it if it's
    still 'queued' (e.g. an earlier invocation died before even claiming) --
    and its current status, if it's already claimed/running/terminal
    (P1-4/P1-5 fix: `main` needs both fields here, unlike the status-only
    `job_status_for_idem` above).

    Best-effort, same contract as `job_status_for_idem`: this is a READ that
    only feeds `main`'s target-selection/exit-code logic, and the run's real
    outcome is independently recorded in `research_jobs`/`research_runs`
    regardless -- a lost read here must never crash the CLI."""
    try:
        sb = _client()
        res = (
            sb.table("research_jobs")
            .select("id, status")
            .eq("idempotency_key", idempotency_key)
            .limit(1)
            .execute()
        )
        if res.data:
            row = res.data[0]
            return {"id": row.get("id"), "status": row.get("status")}
        return None
    except Exception as exc:  # noqa: BLE001 — best-effort read only
        logger.warning("find_job_by_idem: could not read job for %s (%s)", idempotency_key, exc)
        return None


# ---------------------------------------------------------------------------
# Site captures (research_site_captures / research_collection_runs) -- Phase 2
# of the deep-research plan, migrations 142-144. `create_site_capture` and
# `capture_exists` are NOT best-effort (job-spine banner above applies: a
# caller depends on an honest result to decide whether to skip a mint or
# re-attempt one — silently swallowing a failure here could either mint a
# duplicate capture or silently drop a real one). `upload_capture_object` is
# also not best-effort in the sense that it never swallows a REAL storage
# failure, but a "the object already exists" response is expected, normal
# no-clobber behavior, not an error. `start_collection_run`/
# `finish_collection_run` are observability, same best-effort contract as
# `start_run`/`finish_run` above.
# ---------------------------------------------------------------------------

def create_site_capture(
    url: str,
    source_url: str,
    raw_content: str,
    connector: str,
    competitor_id: Optional[str] = None,
    confidence: Optional[str] = None,
    captured_html_path: Optional[str] = None,
    raw_html_sha256: Optional[str] = None,
) -> str:
    """Call the deployed `rs_create_site_capture` RPC (migrations 142-144,
    provenance param added by migration 149): mints one
    `research_site_captures` row and returns its id.

    `p_captured_html_path` is REQUIRED by the RPC itself (it raises if
    null/blank) — the fetched bytes must already be durably stored
    (`upload_capture_object`, storage-first) BEFORE this is ever called; see
    `worker.connectors.web_fetch.capture_pages` for the ordering this
    wrapper depends on its caller following. `content_sha256` is computed
    SERVER-SIDE from `p_raw_content` — this wrapper never computes or sends
    a hash itself. The RPC also forces `verification_status`/`origin` to
    unverified/collected and REFUSES any connector not `status='enabled'` in
    `research_connectors` — `web.fetch` is seeded `status='proposed'` today,
    so a real call raises "not an enabled connector" until a human enables
    it; that is the deployed contract's own behavior, not a bug in this
    wrapper (Phase 2 ships the code; enabling the connector is a separate,
    later, human action).

    `raw_html_sha256` (migration 149) is the sha256 hex of the RAW HTML bytes
    (the same hash the caller already used to key the captured-html object's
    storage path, e.g. `worker.connectors.web_fetch.capture_pages`'s
    `raw_sha256`) — recorded on the capture row itself so downstream
    evidence↔capture provenance can be verified against the exact bytes
    captured, not just the canonical text `content_sha256` already covers.

    NOT best-effort (see the job-spine banner above this section): any RPC
    failure (missing captured_html_path, connector not enabled, DB error)
    propagates unchanged — a caller that silently swallowed this could
    believe a capture was minted when it was not.

    Optional params (`competitor_id`/`confidence`/`captured_html_path`/
    `raw_html_sha256`) are omitted from the RPC payload entirely when None,
    matching every other RPC wrapper's convention in this module
    (`rs_reserve_spend`, `rs_settle_call`).
    """
    sb = _client()
    params: dict = {
        "p_url": url,
        "p_source_url": source_url,
        "p_raw_content": raw_content,
        "p_connector": connector,
    }
    if competitor_id is not None:
        params["p_competitor_id"] = competitor_id
    if confidence is not None:
        params["p_confidence"] = confidence
    if captured_html_path is not None:
        params["p_captured_html_path"] = captured_html_path
    if raw_html_sha256 is not None:
        params["p_raw_html_sha256"] = raw_html_sha256
    res = sb.rpc("rs_create_site_capture", params).execute()
    return res.data


def capture_exists(content_sha256: str, source_url: str) -> bool:
    """True iff a `research_site_captures` row already exists with this
    exact (content_sha256, source_url) pair — the web.fetch connector's
    idempotent-skip check: identical content already recorded for this
    source_url means the correct action is to skip minting and record
    'unchanged', not re-mint a duplicate row.

    NOT best-effort: a caller that treated a failed read here as "does not
    exist" could re-mint a duplicate capture row for content already on
    record, so any failure other than a clean "0 rows" propagates
    unchanged.
    """
    sb = _client()
    res = (
        sb.table("research_site_captures")
        .select("id")
        .eq("content_sha256", content_sha256)
        .eq("source_url", source_url)
        .limit(1)
        .execute()
    )
    return bool(res.data)


def _is_duplicate_object_error(exc: Exception) -> bool:
    """Best-effort classification of a Supabase Storage `StorageApiError` as
    "the object already existed" (expected, no-clobber — return False, do
    not raise) vs. any OTHER storage failure (auth, missing bucket, network
    — must propagate, never be mistaken for a harmless duplicate). Checked
    by `.code`/`.status` first (the structured fields `storage3.exceptions.
    StorageApiError` actually carries) and falls back to a message substring
    only if those are absent, since the exact wire shape of a duplicate-
    object error is not something this repo can verify against a live
    Supabase Storage instance in CI.
    """
    code = str(getattr(exc, "code", "") or "").lower()
    status = str(getattr(exc, "status", "") or "")
    message = str(getattr(exc, "message", "") or exc).lower()
    return (
        "duplicate" in code
        or "already exists" in message
        or (status in ("409", "400") and "exist" in message)
    )


def upload_capture_object(path: str, data: bytes, content_type: str) -> bool:
    """Upload `data` to the `RESEARCH_MEDIA_BUCKET` ("research-media") at
    `path`. NO-CLOBBER: a plain create-only upload (never `upsert`), so an
    object that already exists at `path` is left untouched.

    Returns True iff this call's own upload actually wrote a fresh object;
    False if it already existed — expected and harmless (two different
    pages, or two different runs, can share one `content_sha256`/path, and
    the object's content is already correct since its path IS its hash, so
    re-uploading would be wasted work, never a correctness requirement).

    Any OTHER storage failure (auth, missing bucket, network) is NOT
    swallowed — it propagates unchanged, so a caller never mistakes "the
    upload silently failed for an unrelated reason" for "the object already
    existed".
    """
    from storage3.exceptions import StorageApiError

    sb = _client()
    try:
        sb.storage.from_(RESEARCH_MEDIA_BUCKET).upload(
            path, data, file_options={"content-type": content_type},
        )
        return True
    except StorageApiError as exc:
        if _is_duplicate_object_error(exc):
            return False
        raise


def start_collection_run(connector: str) -> Optional[str]:
    """Record that a connector's collection run has STARTED
    (`research_collection_runs`), mirroring `start_run`/`finish_run`'s
    existing `research_runs` pattern for the ads path — observability for a
    whole page-plan pass, not any single page.

    Best-effort, same contract as `start_run`: any failure (table not yet
    migrated, network blip) must never break or block the worker — log a
    warning and return None. Callers must treat a None run_id as "registry
    unavailable" and no-op the corresponding `finish_collection_run`.
    """
    try:
        sb = _client()
        row = sb.table("research_collection_runs").insert({
            "connector": connector,
            "status": "running",
        }).execute()
        return row.data[0]["id"]
    except Exception as exc:  # noqa: BLE001 — best-effort, must never raise
        logger.warning("start_collection_run: could not record run start for connector=%s (%s)", connector, exc)
        return None


def finish_collection_run(run_id: Optional[str], status: str, records: Optional[int] = None) -> None:
    """Record that a connector's collection run FINISHED. No-op if `run_id`
    is None (start_collection_run already failed/degraded for this run).
    Best-effort, same contract as `finish_run`: any failure here must never
    raise."""
    if run_id is None:
        return
    try:
        sb = _client()
        sb.table("research_collection_runs").update({
            "status": status,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "records": records,
        }).eq("id", run_id).execute()
    except Exception as exc:  # noqa: BLE001 — best-effort, must never raise
        logger.warning(
            "finish_collection_run: could not record run finish for run_id=%s (%s)", run_id, exc,
        )


# ---------------------------------------------------------------------------
# P3 EXTRACTION (the `collect` job) -- read a site capture back, download its stored
# objects, and mint VoC quotes / site-fact findings via the deployed evidence-integrity
# RPCs (migrations 142/146). NONE of these are best-effort (same job-spine banner as the
# site-capture section above): `run_collect` depends on an honest read/mint result to
# decide whether to drop or count a piece of extracted content -- silently swallowing a
# failure here could either mint fabricated evidence as if it succeeded, or hide a real
# infrastructure failure behind a false "row not found".
# ---------------------------------------------------------------------------

def get_site_capture(capture_id: str) -> Optional[dict]:
    """Read one `research_site_captures` row by id: `source_url`, `url`, `competitor_id`,
    `content_sha256`, `captured_html_path`, `connector` -- everything `run_collect` needs
    to locate this capture's stored objects and mint evidence citing the right source.

    Returns None if no row matches this id. NOT best-effort: a missing capture row is a
    real precondition failure for the collect job (there is nothing to extract), so the
    caller must be able to tell "not found" apart from "read failed" -- any failure other
    than a clean "0 rows" (network, auth, DB error) propagates unchanged rather than
    collapsing to the same None a genuine miss returns.
    """
    sb = _client()
    res = (
        sb.table("research_site_captures")
        .select("id, competitor_id, url, source_url, captured_html_path, content_sha256, connector")
        .eq("id", capture_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    return res.data[0]


def collect_done_exists(capture_id: str, extractor_version: str) -> bool:
    """True iff a `collect` job for this exact (capture_id, extractor_version) already reached
    status='done' -- the collect-idempotency marker `run_collect` checks before doing any work, so a
    capture that already completed is never re-extracted (even across a retry with a fresh
    reservation ref). A FAILED collect leaves no 'done' row, so this correctly returns False and the
    retry re-extracts.

    NOT best-effort: a read failure must NOT be guessed as 'not done' (which would re-extract and
    risk duplicate evidence); anything other than a clean query result propagates, failing the job
    so it retries rather than double-minting.
    """
    sb = _client()
    res = (
        sb.table("research_jobs")
        .select("id")
        .eq("job_kind", "collect")
        .eq("status", "done")
        .eq("params->>capture_id", capture_id)
        .eq("params->>extractor_version", extractor_version)
        .limit(1)
        .execute()
    )
    return bool(res.data)


def download_capture_object(path: str) -> bytes:
    """Download the raw bytes stored at `path` (e.g. `captures/<sha>.txt` or
    `captures/<sha>.html`) in `RESEARCH_MEDIA_BUCKET`. The read-side counterpart of
    `upload_capture_object`.

    NOT best-effort: any failure (object missing, auth, network) propagates unchanged --
    `run_collect` has no reasonable fallback for a capture's stored object being
    unreadable, and must fail the job rather than silently extracting from nothing.
    """
    sb = _client()
    return sb.storage.from_(RESEARCH_MEDIA_BUCKET).download(path)


def create_voc_quote(
    quote: str,
    source_url: str,
    raw_content: str,
    connector: str,
    competitor_id: Optional[str] = None,
    store_id: Optional[str] = None,
    quote_type: Optional[str] = None,
    theme: Optional[str] = None,
    product_area: Optional[str] = None,
    confidence: Optional[str] = None,
    source_start: Optional[int] = None,
    source_end: Optional[int] = None,
    capture_id: Optional[str] = None,
) -> str:
    """Call the deployed `rs_create_voc_quote` RPC (migration 142, the 12-arg overload
    with the A-1 verbatim check + source-span params; migration 149 added the trailing
    `p_capture_id` provenance param): mints one `research_voc_quotes` row and returns
    its id.

    `p_raw_content` is the evidence the RPC's own A-1 verbatim check runs against — the
    quote must be a NORMALIZED substring of exactly this text (or, when a source span is
    given, the canonical text at `[source_start, source_end)` must normalize to the
    quote). For a customer-voice quote, the caller (`worker.extract.run_collect`) MUST
    pass the extracted REVIEW text here, never the whole-page canonical text — that is
    what makes "customer voice" structurally enforced rather than a model's say-so.

    `content_sha256` is computed server-side from `p_raw_content` — this wrapper never
    computes or sends a hash itself, matching `create_site_capture`'s convention.

    NOT best-effort (see the job-spine banner above): any RPC failure — a quote that
    fails the A-1 verbatim/span check, an unregistered/non-enabled connector, a DB error
    — propagates unchanged. `run_collect`'s admission control is the one place that
    catches this and counts it as a drop; this wrapper itself must never swallow it,
    or a caller could mistake a rejected mint for a successful one.

    Optional params are omitted from the RPC payload entirely when None, matching every
    other RPC wrapper's convention in this module -- EXCEPT `capture_id`: `p_capture_id`
    is always sent, including as an explicit `None` (-> SQL NULL). The RPC defaults
    `p_capture_id` to null and treats a null the same as an omitted arg (no occurrence
    row recorded), so this is behaviorally identical to omitting it when unset, but
    always sending it keeps the mint call's provenance intent explicit at every call
    site rather than relying on omission to mean "no capture".
    """
    sb = _client()
    params: dict = {
        "p_quote": quote,
        "p_source_url": source_url,
        "p_raw_content": raw_content,
        "p_connector": connector,
    }
    if competitor_id is not None:
        params["p_competitor_id"] = competitor_id
    if store_id is not None:
        params["p_store_id"] = store_id
    if quote_type is not None:
        params["p_type"] = quote_type
    if theme is not None:
        params["p_theme"] = theme
    if product_area is not None:
        params["p_product_area"] = product_area
    if confidence is not None:
        params["p_confidence"] = confidence
    if source_start is not None:
        params["p_source_start"] = source_start
    if source_end is not None:
        params["p_source_end"] = source_end
    params["p_capture_id"] = capture_id
    res = sb.rpc("rs_create_voc_quote", params).execute()
    return res.data


def create_finding(
    finding_kind: str,
    schema_version: int,
    payload: dict,
    source_url: str,
    raw_content: str,
    connector: str,
    project_id: Optional[str] = None,
    store_id: Optional[str] = None,
    confidence: Optional[str] = None,
    capture_id: Optional[str] = None,
) -> str:
    """Call the deployed `rs_create_finding` RPC (migration 033, registry-gated by
    migration 146's `('site_fact', 1)` seed; migration 149 added the trailing
    `p_capture_id` provenance param): mints one `research_findings` row and returns
    its id.

    Refuses any `(finding_kind, schema_version)` not already present in
    `research_finding_kinds` (defense in depth ahead of the table's own FK) and any
    non-enabled connector — same legal-gate discipline as `create_voc_quote`/
    `create_site_capture`. `content_sha256` is computed server-side from `p_raw_content`.

    For a `site_fact` finding, `payload` is `{fact_type, statement, evidence, detail?}` —
    `evidence` is a REQUIRED, first-class verbatim excerpt of `raw_content` (the caller,
    `worker.extract._mint_findings`, both requires it non-blank and normalized-substring
    grounding-gates it against the canonical page text before ever calling this wrapper;
    a companion DB-side gate re-runs the same verbatim-present check server-side), never
    folded into `detail` — `detail` remains the model's OPTIONAL free-text elaboration.
    Persisting `evidence` gives `worker.verify`'s finding branch an authoritative verbatim
    span to re-check later, exactly like a VoC quote's `quote` text. The caller
    (`worker.extract.run_collect`) passes the page's own CANONICAL text as `raw_content`
    — a finding is a page fact, not customer voice, so it is evidenced against the whole
    page, not the review-widget text.

    NOT best-effort: any RPC failure (unregistered kind/version, non-enabled connector,
    DB error) propagates unchanged; `run_collect`'s admission control is the one place
    that catches this and counts it as a drop.

    Optional params are omitted from the RPC payload entirely when None, matching every
    other RPC wrapper's convention in this module -- EXCEPT `capture_id`, always sent as
    `p_capture_id` (including `None` -> SQL NULL), same rationale as `create_voc_quote`.
    """
    sb = _client()
    params: dict = {
        "p_finding_kind": finding_kind,
        "p_schema_version": schema_version,
        "p_payload": payload,
        "p_source_url": source_url,
        "p_raw_content": raw_content,
        "p_connector": connector,
    }
    if project_id is not None:
        params["p_project_id"] = project_id
    if store_id is not None:
        params["p_store_id"] = store_id
    if confidence is not None:
        params["p_confidence"] = confidence
    params["p_capture_id"] = capture_id
    res = sb.rpc("rs_create_finding", params).execute()
    return res.data


# ---------------------------------------------------------------------------
# P4 VERIFICATION (the `verify` job) -- thin wrappers over the deployed evidence-
# verification RPCs (migration 150) plus two read-back helpers for the evidence rows a
# frozen sample member points at. Same job-spine banner as P3 EXTRACTION above: NONE of
# these are best-effort. `worker.verify.run_verify` depends on an honest read/RPC result
# to decide a member's outcome or the capture's verdict -- silently swallowing a failure
# here could either record a fabricated 'supported' verdict or hide a real infrastructure
# failure behind a false "nothing to verify". Every RPC wrapper below is fenced
# server-side on (job_kind='verify', claimed_by=p_claim_token, status='running') exactly
# like `rs_reserve_spend`/`rs_settle_call` are fenced on the collect/scrape job spine --
# a stale/revoked claim raises from the RPC itself, and that raise propagates unchanged.
# ---------------------------------------------------------------------------

def rs_verify_freeze_sample(job_id: str, claim_token: str) -> list:
    """Call the deployed `rs_verify_freeze_sample` RPC: freezes (or, on a replay within
    the same claim, re-returns) this verify job's sample of `research_verify_sample_members`
    rows -- each `{job_id, evidence_kind, evidence_id, capture_id, sample_rank,
    result='pending'}` (a member already terminalized by an EARLIER call -- e.g. a replay
    after `rs_verify_record_integrity` already bulk-terminalized some rows -- comes back
    with its current, non-'pending' `result`, not reset to 'pending').

    Idempotent: re-calling with the same `(job_id, claim_token)` returns the SAME frozen
    set rather than re-sampling, so a crash-replay within one claim never draws a
    different sample out from under an in-progress verify run.

    Returns a plain list -- possibly EMPTY. A 0-member sample is a legitimate, valid
    outcome (nothing matched this job's verification scope), never an error: `worker.
    verify.run_verify` still runs the capture-integrity check and finalizes even when
    this returns `[]`, because `rs_verify_finalize` authenticates the CAPTURE regardless
    of how many evidence rows happened to land in the sample.

    NOT best-effort: any RPC failure (the fence rejecting a stale/wrong claim_token, a
    job_id that isn't a 'verify' job, a DB error) propagates unchanged.
    """
    sb = _client()
    res = sb.rpc(
        "rs_verify_freeze_sample", {"p_job_id": job_id, "p_claim_token": claim_token},
    ).execute()
    return res.data or []


def rs_verify_record_integrity(
    job_id: str,
    claim_token: str,
    observed_txt_sha256: Optional[str],
    observed_html_sha256: Optional[str],
    txt_missing: bool,
    html_missing: bool,
) -> dict:
    """Call the deployed `rs_verify_record_integrity` RPC: records the up-front
    capture-integrity result for this verify job's `verify_capture_id`, comparing the
    CALLER-OBSERVED hashes of the freshly re-downloaded canonical `.txt` and raw `.html`
    storage objects against the capture row's own recorded hashes.

    STRICT input rule enforced RPC-side (not re-validated here -- the RPC is the real
    guarantee, this wrapper never second-guesses it): per object, exactly ONE of
    `(observed_..._sha256=<64-hex>, ..._missing=False)` or
    `(observed_..._sha256=None, ..._missing=True)` must hold, or the RPC raises
    `invalid_integrity_report`. `worker.verify.run_verify` is the caller responsible for
    upholding this (see its `_download_hash_or_missing` helper).

    The RPC alone decides ok/mismatch/object_missing/unauthenticatable and bulk-
    terminalizes this job's pending sample members accordingly (a mismatch or missing
    object marks every still-pending member 'integrity_failed'; an unauthenticatable
    capture -- its own `content_sha256`/`raw_html_sha256` is null/malformed on the capture
    row itself -- marks them 'unauthenticatable'). This wrapper's caller does NOT branch
    on the RPC's own verdict beyond passing the observed hashes/flags through -- it simply
    re-reads member status afterward (`get_verify_sample_members`) to see what, if
    anything, is still 'pending'.

    Called exactly ONCE per verify run, up-front, immediately after `rs_verify_freeze_
    sample` -- never per-member, and never skipped even for a 0-member sample (a 0-member
    sample must still authenticate the capture, since `rs_verify_finalize` refuses to run
    until integrity has been recorded).

    NOT best-effort: any RPC failure (the fence, `invalid_integrity_report`, a DB error)
    propagates unchanged.
    """
    sb = _client()
    params = {
        "p_job_id": job_id,
        "p_claim_token": claim_token,
        "p_observed_txt_sha256": observed_txt_sha256,
        "p_observed_html_sha256": observed_html_sha256,
        "p_txt_missing": txt_missing,
        "p_html_missing": html_missing,
    }
    res = sb.rpc("rs_verify_record_integrity", params).execute()
    return res.data


def rs_verify_record_outcome(
    job_id: str,
    claim_token: str,
    evidence_kind: str,
    evidence_id: str,
    result: str,
    check_detail: Optional[dict] = None,
) -> dict:
    """Call the deployed `rs_verify_record_outcome` RPC: records the per-occurrence
    ledger outcome for ONE frozen sample member -- `result` MUST be one of
    `'supported' | 'unsupported' | 'abstained'`.

    RPC-side fencing (not re-validated here) requires: the claim fence holds, integrity
    was already recorded for this job AND both objects came back 'ok' (a member the
    integrity step itself already terminalized -- 'integrity_failed'/'unauthenticatable'
    -- is NOT a legal target here; `worker.verify.run_verify` must never call this for a
    member it did not itself find still 'pending'), the `(evidence_kind, evidence_id)`
    pair is actually in this job's frozen sample, and the member is currently 'pending'
    (pending -> terminal exactly once; a repeat call for an already-terminal member
    raises rather than silently overwriting a recorded verdict).

    `check_detail` defaults to `{}` (never `None` on the wire) -- the RPC's jsonb column
    is NOT NULL. Pass whatever the caller's own check produced: for a deterministic
    verbatim/grounding failure, `{"reason": "verbatim_failed", ...}`; for the adversarial
    label pass, `{"model": ..., "rationale_hash": ..., "input_tokens": ...,
    "output_tokens": ...}` (or a `"reason"` key for an abstain).

    NOT best-effort: any RPC failure (the fence, "not in sample", "already terminal", a
    DB error) propagates unchanged -- `run_verify`'s per-member loop is the one place
    that decides what a given failure means, this wrapper never swallows it.
    """
    sb = _client()
    params = {
        "p_job_id": job_id,
        "p_claim_token": claim_token,
        "p_evidence_kind": evidence_kind,
        "p_evidence_id": evidence_id,
        "p_result": result,
        "p_check_detail": check_detail if check_detail is not None else {},
    }
    res = sb.rpc("rs_verify_record_outcome", params).execute()
    return res.data


def rs_verify_finalize(job_id: str, claim_token: str) -> dict:
    """Call the deployed `rs_verify_finalize` RPC: computes this verify job's CAPTURE
    verdict from the recorded integrity result + the per-member outcome ledger, derives
    and stamps every sampled evidence row's `verification_status` from that ledger, and
    sets the job itself 'done'.

    Refuses (raises) while any frozen sample member is still 'pending', and refuses if
    integrity was never recorded at all -- `worker.verify.run_verify` must always call
    `rs_verify_record_integrity` first and drive every member to a terminal result before
    calling this, even for a 0-member sample (integrity-only capture authentication).

    Idempotent: a replay (same job_id, same claim_token, job already 'done') returns the
    SAME stored verdict rather than recomputing or erroring -- a crash-replay after a
    successful finalize is safe to re-run.

    NOT best-effort: any RPC failure (the "still pending" / "integrity not recorded"
    refusals, the fence, a DB error) propagates unchanged -- `run_verify` has no
    reasonable fallback for a finalize that didn't actually happen.
    """
    sb = _client()
    res = sb.rpc(
        "rs_verify_finalize", {"p_job_id": job_id, "p_claim_token": claim_token},
    ).execute()
    return res.data


def get_verify_sample_members(job_id: str, result: Optional[str] = None) -> list:
    """Read back this verify job's `research_verify_sample_members` rows -- `{job_id,
    evidence_kind, evidence_id, capture_id, sample_rank, result}` each -- optionally
    filtered to one `result` value (e.g. `result="pending"`, the caller's usual need: find
    what is left to process after `rs_verify_record_integrity` may have bulk-terminalized
    some rows).

    A plain table SELECT, not an RPC -- there is no fencing concern on a READ. Used by
    `worker.verify.run_verify` to re-discover which members are still 'pending' after the
    up-front integrity call (the RPC's own bulk-terminalize doesn't hand back an updated
    member list, only its own capture-level verdict), and to tally final per-result counts
    for the job's returned summary.

    NOT best-effort: `run_verify` depends on an honest read to decide which members to
    process next; any failure other than a clean (possibly empty) result set propagates
    unchanged.
    """
    sb = _client()
    query = (
        sb.table("research_verify_sample_members")
        .select("job_id, evidence_kind, evidence_id, capture_id, sample_rank, result")
        .eq("job_id", job_id)
    )
    if result is not None:
        query = query.eq("result", result)
    res = query.execute()
    return res.data or []


def get_voc_quote(quote_id: str) -> Optional[dict]:
    """Read one `research_voc_quotes` row by id -- `quote`, `type`, `theme`,
    `product_area`, `confidence`, `source_url`, `capture_id` -- everything `worker.
    verify.run_verify` needs to re-check a 'voc' sample member's quote text against a
    freshly re-extracted, hash-authenticated review text, and to build the adversarial
    label pass's LABEL payload.

    Returns None if no row matches this id. NOT best-effort: a missing quote row for a
    frozen sample member is a real anomaly (the sample member references it by id), not a
    silent "nothing to verify" -- the caller decides how to score that (abstain, never a
    false 'unsupported'), it is not this wrapper's call to make.
    """
    sb = _client()
    res = (
        sb.table("research_voc_quotes")
        .select("id, quote, type, theme, product_area, confidence, source_url, capture_id")
        .eq("id", quote_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    return res.data[0]


# ---------------------------------------------------------------------------
# P4 PRODUCTION (the `synthesize` job) -- read back the currently-publishable evidence
# for a store and mint a DRAFT, evidence-linked synthesis through the deployed
# `rs_create_synthesis` RPC (migration 157). Same job-spine banner as P3 EXTRACTION /
# P4 VERIFICATION above: neither wrapper below is best-effort. `worker.synthesize.
# run_synthesize` depends on an honest read to know what it may cite, and an honest RPC
# result to know whether the synthesis it built was actually minted -- silently
# swallowing either could either cite/publish evidence nobody confirmed is publishable,
# or hide a real infrastructure failure behind a false "nothing to synthesize".
# ---------------------------------------------------------------------------

def get_publishable_evidence(store_id: str, limit: Optional[int] = None) -> list:
    """Read `research_publishable_evidence` (migration 150 VIEW) rows for `store_id` --
    `evidence_id, evidence_kind, summary` -- the ONLY source of citable evidence a
    `synthesize` job may draw from. The view carries no `project_id`/`area` column at
    all (confirmed against the deployed schema), so `store_id` is the only scope it
    supports -- there is nothing finer to filter on here; a caller wanting a narrower
    slice has no query-side way to get one.

    `limit` bounds how many rows come back (server-side, via `.limit()`) -- `worker.
    synthesize.run_synthesize` uses this to keep its prompt (and therefore its
    worst-case cost reservation) bounded regardless of how much publishable evidence a
    store has accumulated. `None` (the default) applies no limit, matching every other
    optional-param convention in this module.

    Returns a plain list -- possibly EMPTY, which is a legitimate outcome (nothing
    publishable yet for this store): `run_synthesize` refuses to synthesize from an
    empty set rather than treating this as a read failure, and never invents a
    synthesis to cover for it.

    NOT best-effort (see the job-spine banner above this section): any read failure
    (network, auth, DB error) propagates unchanged -- a caller that silently treated a
    failed read as "no evidence" could wrongly refuse a synthesize job that should have
    succeeded, or (worse) proceed on a citation set nobody actually confirmed is real.
    """
    sb = _client()
    query = (
        sb.table("research_publishable_evidence")
        .select("evidence_id, evidence_kind, summary")
        .eq("store_id", store_id)
    )
    if limit is not None:
        query = query.limit(limit)
    res = query.execute()
    return res.data or []


def rs_create_synthesis(
    store_id: str,
    kind: str,
    schema_version: int,
    title: str,
    payload: dict,
    evidence_refs: list,
    confidence: str,
    area: Optional[str] = None,
    project_id: Optional[str] = None,
    origin: str = "agent",
    thin_data: bool = False,
    created_by: Optional[str] = None,
) -> str:
    """Call the deployed `rs_create_synthesis` RPC (migration 157): mints one
    `research_syntheses` row and returns its id.

    The RPC RE-CHECKS every `evidence_refs` entry server-side against non-refuted,
    currently-publishable evidence and FORCES `status='draft'` regardless of what is
    passed here -- this worker NEVER publishes; `rs_publish_synthesis` is a separate,
    human (L3) action this module never calls, directly or indirectly.

    `p_evidence_refs` is a jsonb array of `{"table": <'research_voc_quotes'|
    'research_findings'>, "id": <evidence_id>}` -- the DISTINCT set of evidence the
    caller (`worker.synthesize.run_synthesize`) actually validated and cited, built
    from `get_publishable_evidence`'s own returned rows, never invented here.

    NOT best-effort (see the job-spine banner above this section): any RPC failure (a
    ref that went refuted/non-publishable between the caller's read and this call, a
    malformed payload, a DB error) propagates unchanged -- `run_synthesize` is the one
    place that decides what a rejection means (a `("failed", ...)` return, spend already
    settled), this wrapper never swallows it.

    Optional params (`area`/`project_id`/`created_by`) are omitted from the RPC payload
    entirely when None, matching every other RPC wrapper's convention in this module.
    `origin`/`thin_data` are always sent -- they carry real, meaningful defaults rather
    than "omit when absent" semantics (the RPC has no sensible default for either that
    this wrapper should silently defer to).
    """
    sb = _client()
    params: dict = {
        "p_store_id": store_id,
        "p_kind": kind,
        "p_schema_version": schema_version,
        "p_title": title,
        "p_payload": payload,
        "p_evidence_refs": evidence_refs,
        "p_confidence": confidence,
        "p_origin": origin,
        "p_thin_data": thin_data,
    }
    if area is not None:
        params["p_area"] = area
    if project_id is not None:
        params["p_project_id"] = project_id
    if created_by is not None:
        params["p_created_by"] = created_by
    res = sb.rpc("rs_create_synthesis", params).execute()
    return res.data


def get_finding(finding_id: str) -> Optional[dict]:
    """Read one `research_findings` row by id -- `finding_kind`, `schema_version`,
    `payload` (jsonb: `fact_type`/`statement`/`evidence`/optional `detail` for a
    `site_fact` v1 row -- `evidence` is the first-class, DB-gated verbatim excerpt
    `worker.verify`'s finding branch re-checks against a freshly re-downloaded canonical
    text), `source_url`, `capture_id`, `confidence` -- everything `worker.verify.run_verify`
    needs to re-check a 'finding' sample member's grounding against a freshly
    re-extracted, hash-authenticated canonical text, and to build the adversarial label
    pass's LABEL payload.

    Returns None if no row matches this id. NOT best-effort, same reasoning as
    `get_voc_quote` above.
    """
    sb = _client()
    res = (
        sb.table("research_findings")
        .select("id, finding_kind, schema_version, payload, source_url, capture_id, confidence")
        .eq("id", finding_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    return res.data[0]
