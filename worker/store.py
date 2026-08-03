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
