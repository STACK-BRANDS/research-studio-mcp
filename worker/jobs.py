"""Job-spine primitives for the Research Studio worker's `research_jobs`
queue consumer (Phase 1, tasks 1.1-1.3 of the deep-research plan):

  1.1 Deterministic, project/wave-scoped idempotency-key builders (R4) --
      every key is prefixed with the owning project id so two projects
      running the same competitor/day/url never collide on the UNIQUE
      `idempotency_key` column. Ad-hoc (non-project) work uses the literal
      project id `ADHOC_PROJECT` ("adhoc") as that prefix. Every time/wave
      component is PASSED IN by the caller -- these builders never call
      the clock themselves, so the same call always yields the same key.

  1.2 CAS claim/lease/heartbeat over the deployed `rs_claim_job` RPC
      (architecture §13 R1) -- `make_claimant()` mints a fencing token that
      `claim_next()` writes into `claimed_by`; every later mutation
      (`mark_running`, `heartbeat`, `assert_lease`, `finish_job`) CASes on
      `claimed_by = <that same token>`. Zero rows affected means the lease
      was revoked -- by the reaper, or (in a misbehaving deployment) a
      second claimant -- and the caller must abort without any further
      side effect: no write, no spend, no child-job enqueue.

  1.3 The stale-claim reaper -- re-queues jobs whose lease has gone cold
      (claimed_at older than that job_kind's configured timeout) so a
      crashed or killed worker's job becomes claimable again. A worker
      that calls `heartbeat()` on schedule keeps its lease fresh and is
      never reaped.

  1.6 The ads-as-scrape handler (`worker.run.handle_scrape`) -- reuses the
      existing single-shot ads orchestration UNCHANGED. It is dispatched
      from here (`_dispatch`), not implemented here.

  1.7 `enqueue()` (a thin wrapper over `store.enqueue_job`) and `drain()` --
      the consumer poll loop: reap -> claim (a FRESH fencing token per
      claim, never reused across iterations) -> mark_running -> heartbeat ->
      dispatch -> finish, repeated until the queue is empty (`once=True`) or
      forever (`once=False`). The per-job lifecycle (mark_running ->
      heartbeat -> dispatch -> finish) is shared, via `_run_claimed()`, with
      `run_one()` -- the CLI's "claim and run exactly this one job_id"
      entrypoint, which never drains the whole queue.

Concurrency note (no advisory-lock single-consumer): an earlier draft of
this architecture (S2-1) proposed a `pg_advisory_lock` to enforce a single
worker. That is infeasible here and was deliberately dropped: a
SESSION-level advisory lock cannot be held across separate supabase-py
calls, because PostgREST is stateless HTTP -- every call opens a fresh
connection, so a lock taken for one request is already gone before the
next request's connection even opens. Safety does not depend on it: the
per-job CAS claim (`rs_claim_job`'s `FOR UPDATE SKIP LOCKED`) plus the
lease-fence CAS already built into every mutation above (`claimed_by =
claimant`) make concurrent workers safe BY CONSTRUCTION -- two workers
claiming concurrently get two DIFFERENT jobs, never the same one, and a
worker that gets reaped mid-run has every further write silently rejected
(0 rows matched) rather than clobbering whatever now owns the row. v1 runs
a single worker by OPERATING CONVENTION, not a DB lock -- nothing here
requires it, but nothing here coordinates two workers' WORK either (only
their writes), so that remains a documented, not-yet-supported topology.

Budget/spend accrual, connectors, extraction, verification and the
scrape -> collect -> verify -> synthesize chain driver are later tasks --
this module is the concurrency core only.
"""
import logging
import os
import random
import socket
import string
import threading
import time
import uuid
from typing import Optional

from worker import store
from worker.config import settings

logger = logging.getLogger(__name__)

ADHOC_PROJECT = "adhoc"


# ---------------------------------------------------------------------------
# 1.1 -- deterministic idempotency-key builders
# ---------------------------------------------------------------------------

def idem_scrape_ads(project: str, competitor: str, yyyymmdd: str) -> str:
    return f"scrape:ads:{project}:{competitor}:{yyyymmdd}"


def idem_scrape_web(project: str, competitor: str, url_sha: str, wave: str) -> str:
    return f"scrape:web.fetch:{project}:{competitor}:{url_sha}:{wave}"


def idem_collect(project: str, capture_sha: str, extractor_version: str) -> str:
    return f"collect:{project}:{capture_sha}:{extractor_version}"


def idem_verify(project: str, scope_sha: str, verifier_version: str) -> str:
    return f"verify:{project}:{scope_sha}:{verifier_version}"


def idem_synthesize(project: str, store: str, area: str, kind: str, evidence_set_sha: str) -> str:
    return f"synthesize:{project}:{store}:{area}:{kind}:{evidence_set_sha}"


def with_attempt(key: str, attempt: int) -> str:
    """Bounded retry of a terminally-failed job: append `:a<attempt>`
    (attempt>=1) so a re-enqueued retry gets its OWN idempotency_key rather
    than colliding on insert with the failed row's key -- which would just
    conflict instead of creating a fresh queued attempt."""
    return f"{key}:a{attempt}"


# ---------------------------------------------------------------------------
# 1.2 -- claim + lease + heartbeat
# ---------------------------------------------------------------------------

def make_claimant() -> str:
    """Mint a fencing token unique to this one claim attempt:
    "<hostname>:<pid>:<runid>:<nonce>". Every mutation on a job this token
    claims CASes on `claimed_by = <token>` -- once the reaper (or a second
    claimant, in a misbehaving deployment) overwrites/clears `claimed_by`,
    this exact string can never match again, so every further heartbeat,
    write, or finish call for that job safely returns False instead of
    touching a row this worker no longer owns.

    hostname+pid alone would collide across restarts of the same container
    (many schedulers reuse pid 1) and across two containers sharing a
    hostname (e.g. identical Cloud Run Job revision names) -- `runid` plus a
    random `nonce` make every single claim attempt's token unique, even
    across two claims made back-to-back by the same process."""
    hostname = socket.gethostname()
    pid = os.getpid()
    runid = uuid.uuid4().hex[:8]
    nonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"{hostname}:{pid}:{runid}:{nonce}"


def claim_next(kinds: Optional[list], claimant: str) -> Optional[dict]:
    """Claim the next queued job via the deployed `rs_claim_job` RPC
    (`FOR UPDATE SKIP LOCKED` runs server-side -- the concurrency guarantee
    is the database's, not Python's).

    If `kinds` is given, tries each kind IN THE GIVEN ORDER and returns the
    first one with a queued row (a worker specialised in e.g.
    `["verify", "collect"]` drains its preferred kind first, falling back
    to the next only when the preferred kind is currently empty). If
    `kinds` is None, makes a single kind-unfiltered call and lets
    `rs_claim_job`'s own oldest-queued-first ordering decide across every
    kind.

    Returns the claimed row, or None if nothing was queued for any given
    kind (or, when `kinds` is None, nothing was queued at all)."""
    if kinds is None:
        return store.claim_job(None, claimant)
    for kind in kinds:
        job = store.claim_job(kind, claimant)
        if job is not None:
            return job
    return None


def mark_running(job_id: str, claimant: str) -> bool:
    """CAS claimed -> running. True iff this claimant still held the claim;
    False means the lease was revoked before the handler even started --
    the caller must abort without running the handler at all."""
    return store.mark_job_running(job_id, claimant)


def heartbeat(job_id: str, claimant: str) -> bool:
    """Bump claimed_at so a live, still-working claimant is never reaped.
    Call periodically (within the kind's `settings.job_lease_timeouts`
    budget) while a handler runs. True iff the lease is still held; False
    means it was already revoked -- the handler must stop and abort."""
    return store.heartbeat_job(job_id, claimant)


def assert_lease(job_id: str, claimant: str) -> bool:
    """The pre-side-effect fence: call this immediately before ANY external
    write or spend inside a handler. False means the lease is gone (reaped
    or otherwise revoked) and the handler must abort immediately, doing
    nothing -- no partial write, no partial spend."""
    return store.job_lease_held(job_id, claimant)


def finish_job(
    job_id: str,
    claimant: str,
    status: str,
    error: Optional[str] = None,
    cost_cents: Optional[int] = None,
    result: Optional[dict] = None,
) -> bool:
    """CAS terminal transition running -> done|failed. True iff this
    claimant still held the lease at the moment of finishing; False means
    the lease was revoked mid-run (the reaper already re-queued the job as
    a fresh attempt) and the caller's result must be discarded rather than
    written over whatever now owns the row."""
    return store.finish_job(job_id, claimant, status, error=error, cost_cents=cost_cents, result=result)


# ---------------------------------------------------------------------------
# 1.3 -- stale-claim reaper
# ---------------------------------------------------------------------------

def reap_stale() -> int:
    """Re-queue every job whose claim has gone stale: for each `job_kind`,
    any row still `claimed`/`running` with `claimed_at` older than that
    kind's `settings.job_lease_timeouts` entry is reset to
    `status='queued', claimed_by=NULL` (one UPDATE per kind, done entirely
    server-side -- no read-then-write race). A worker that calls
    `heartbeat()` on schedule keeps its `claimed_at` fresh and is never
    matched by this filter; only a genuinely dead worker's job is
    re-queued.

    Call at the top of each consumer poll (the poll loop itself is a later
    task -- this is the reaper as a library function). Returns the total
    number of rows re-queued across all kinds."""
    total = 0
    for kind, timeout_seconds in settings.job_lease_timeouts.items():
        total += store.reap_stale_jobs(kind, timeout_seconds)
    return total


# ---------------------------------------------------------------------------
# 1.7 -- enqueue + heartbeat-while-running + dispatch + the drain/poll loop
# ---------------------------------------------------------------------------

def enqueue(
    job_kind: str,
    params: dict,
    idempotency_key: str,
    project_id: Optional[str] = None,
    competitor_id: Optional[str] = None,
    store_id: Optional[str] = None,
) -> Optional[str]:
    """Thin wrapper over `store.enqueue_job`: insert a new `research_jobs`
    row, or silently no-op (return None) when `idempotency_key` already
    exists. See `store.enqueue_job`'s docstring for exactly how the
    ON-CONFLICT-DO-NOTHING is implemented (an `upsert(...,
    ignore_duplicates=True)`, not a caught 23505)."""
    return store.enqueue_job(
        job_kind,
        params,
        idempotency_key,
        project_id=project_id,
        competitor_id=competitor_id,
        store_id=store_id,
    )


class _Heartbeat:
    """Background daemon thread that calls `heartbeat(job_id, claimant)`
    every `interval` seconds while a handler runs, so a live worker's lease
    is never mistaken for dead by `reap_stale()` (decision C). Always
    stopped -- via `.stop()`, in a `finally` around the handler dispatch in
    `drain()` -- whether the handler returned normally or raised.

    `heartbeat_fn` and `wait_fn` are both injectable so a test can drive the
    tick loop (`_loop`) directly and deterministically, with NO real thread
    and NO real sleep: a fake `wait_fn` can return False (keep ticking) a
    fixed number of times then True (stop signal received) on a plain
    synchronous call to `_loop()`, never touching `.start()`/a real
    `threading.Thread` at all. `wait_fn` defaults to the real stop event's
    own `.wait(timeout)` (returns True once `.stop()` sets the event, False
    on each timeout with nothing to report) when actually run via
    `.start()`.

    If `heartbeat_fn` returns False, the lease was already revoked (reaped,
    or -- in a misbehaving deployment -- claimed by someone else); this
    logs a warning and stops ticking. It does NOT raise: `finish_job`'s own
    CAS is the real backstop -- once the lease is gone, `finish_job` will
    correctly return False too and the handler's eventual result is
    discarded, matching the reaper's already-requeued fresh attempt. A
    `heartbeat_fn` call that itself raises is treated the same way (logged,
    ticking stops) rather than propagating out of a background thread,
    where an uncaught exception would otherwise just vanish to stderr with
    no chance for `drain()` to react.
    """

    def __init__(self, job_id: str, claimant: str, interval: float,
                 heartbeat_fn=None, wait_fn=None) -> None:
        self._job_id = job_id
        self._claimant = claimant
        self._interval = interval
        self._heartbeat_fn = heartbeat_fn if heartbeat_fn is not None else heartbeat
        self._stop_event = threading.Event()
        self._wait_fn = wait_fn if wait_fn is not None else self._stop_event.wait
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._wait_fn(self._interval):
            try:
                ok = self._heartbeat_fn(self._job_id, self._claimant)
            except Exception as exc:  # noqa: BLE001 -- never let a background thread's
                # exception escape uncaught; treat it the same as a revoked lease.
                logger.warning(
                    "heartbeat call raised for job %s (claimant %s): %s -- stopping "
                    "heartbeat thread", self._job_id, self._claimant, exc,
                )
                return
            if not ok:
                logger.warning(
                    "heartbeat lease lost for job %s (claimant %s) -- stopping "
                    "heartbeat thread; finish_job's CAS will discard this handler's "
                    "eventual result", self._job_id, self._claimant,
                )
                return

    def stop(self, join_timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._thread.join(timeout=join_timeout)


def _spawn_heartbeat(job_id: str, claimant: str, job_kind: Optional[str]) -> _Heartbeat:
    """Start (already-started) the heartbeat thread for one running handler
    invocation. Interval is `min(timeout // 3, 30)` seconds (decision C),
    where `timeout` is `job_kind`'s configured `settings.job_lease_timeouts`
    entry (falling back to 300s for a job_kind somehow absent from that
    map -- the CHECK-locked {scrape, collect, verify, synthesize} set and
    `job_lease_timeouts`'s own test coverage mean this fallback should never
    actually trigger for a real job).

    Split out from `_Heartbeat` itself as its own seam so a `drain()` test
    can monkeypatch JUST this one call to a fake, no-op start/stop stub --
    exercising the real claim/mark_running/dispatch/finish sequence around
    it without ever spinning a real thread."""
    timeout = settings.job_lease_timeouts.get(job_kind, 300)
    interval = min(timeout // 3, 30)
    return _Heartbeat(job_id, claimant, interval).start()


def _dispatch(job: dict, claimant: str) -> tuple:
    """Route one claimed+running job to its handler by (job_kind,
    connector). Only one real handler exists in Phase 1: (scrape,
    ad_library.scrapecreators) -> `worker.run.handle_scrape` (Task 1.6, the
    ads-as-scrape reuse). Every other (job_kind, connector) combination --
    collect/verify/synthesize, and any scrape connector besides the ads one
    -- has no handler yet (Tasks P2-P4): it is finished 'failed' with a
    clear, machine-greppable error rather than raising or crashing the
    loop, so an as-yet-unimplemented job_kind can already be queued (e.g. by
    a future planner) without taking down a worker that doesn't know how to
    run it yet.

    Returns `(status, cost_cents, error)` -- `_run_claimed()` passes these
    straight through to `finish_job`. The scrape branch's `error` is
    `handle_scrape`'s own (P2-2 fix: no longer discarded to None on a
    failure) -- passed through here unchanged, not re-derived.

    `worker.run` is imported HERE, inside the function, rather than at
    module level, specifically to avoid a circular import: `worker.run`
    imports `worker.jobs` at module level (for `enqueue`/`drain`/
    `make_claimant` in its CLI `main()`), and this module dispatches back
    into `worker.run.handle_scrape` -- a module-level `from worker import
    run` on THIS side as well would make the two modules' load order matter
    (whichever is imported first would see the other only partially
    initialized). Deferring the import to call time sidesteps the ordering
    question entirely: by the time `drain()` actually dispatches a job, both
    modules have long finished importing.
    """
    job_kind = job.get("job_kind")
    params = job.get("params") or {}
    connector = params.get("connector") if isinstance(params, dict) else None

    if job_kind == "scrape" and connector == "ad_library.scrapecreators":
        from worker import run  # noqa: PLC0415 -- deferred; see docstring above.
        status, cost_cents, error = run.handle_scrape(job, claimant)
        return status, cost_cents, error

    return "failed", None, "handler not implemented (P2-P4)"


def _run_claimed(job: dict, claimant: str) -> Optional[str]:
    """The per-job lifecycle shared by `drain()` and `run_one()`:
    mark_running -> spawn heartbeat -> dispatch (try/finally) -> finish_job.
    Extracted (P1-3/P1-4 fix) so both callers run the EXACT same sequence
    rather than one reimplementing what the other already does.

    Returns the PERSISTED terminal status ('done' or 'failed') -- i.e. only a
    status `finish_job`'s CAS actually committed. Returns `None` in either case
    where no outcome was persisted for this claim: (1) `mark_running` failed --
    the lease was already revoked between the caller's claim and here (another
    reaper pass; a misbehaving second claimant), so the job is skipped entirely
    (no heartbeat, no dispatch, no finish_job call); or (2) `finish_job`'s CAS
    returned False -- the lease was revoked after dispatch but before finishing,
    so the write was discarded and the reaper has already re-queued a fresh
    attempt for a new claimant. Because `None` means "no authoritative outcome
    for this claim", callers (`drain()`'s count, `run_one()`/`main()`'s CLI exit
    code) never mistake an unpersisted 'done'/'failed' for this run's result.

    `_spawn_heartbeat` starts a background heartbeat thread BEFORE the
    handler dispatches, and it is ALWAYS stopped in a `finally` -- whether
    `_dispatch` returned normally or raised -- so a lease is never left
    silently heartbeating once its handler is done. A raised exception from
    `_dispatch` itself (a handler that violated its own contract of never
    raising) is caught here too and converted into a 'failed' finish rather
    than propagating -- the same "one job's failure must fail only that
    job" discipline the "handler not implemented" branch already gets
    inside `_dispatch`.
    """
    job_id = job["id"]
    if not mark_running(job_id, claimant):
        # Lease revoked between the claim and here -- skip; nothing
        # further is done to this job on this pass.
        return None

    job_kind = job.get("job_kind")
    hb = _spawn_heartbeat(job_id, claimant, job_kind)
    try:
        status, cost_cents, error = _dispatch(job, claimant)
    except Exception as exc:  # noqa: BLE001 -- a handler must never crash the caller
        status, cost_cents, error = "failed", None, f"handler raised: {exc}"
    finally:
        hb.stop()

    # finish_job's own CAS is the final word: if the lease was revoked after
    # dispatch but before here, it matches 0 rows and returns False -- the
    # outcome was NOT persisted (the reaper already re-queued the job for a new
    # claimant, who is now authoritative). Return None in that case so the
    # caller (run_one/main's exit code) never treats an unpersisted 'done' or
    # 'failed' as this run's authoritative outcome.
    if not finish_job(job_id, claimant, status, error=error, cost_cents=cost_cents):
        return None
    return status


def drain(
    kinds: Optional[list] = None,
    once: bool = False,
    poll_interval: float = 2.0,
) -> int:
    """The Task 1.7 consumer poll loop, tying every earlier job-spine
    primitive together: reap -> claim -> mark_running -> heartbeat ->
    dispatch -> finish, repeated until told to stop. Returns the number of
    jobs this call actually processed (claimed + `mark_running` succeeded +
    dispatched to a handler), regardless of whether each one finished
    'done' or 'failed'.

    Each iteration: `reap_stale()` runs FIRST -- a dead worker's stale claim
    is re-queued before the next claim attempt, so a fresh claim is never
    stuck behind a corpse. Then a FRESH fencing token is minted via
    `make_claimant()` -- P1-3 fix: earlier, one `claimant` argument was
    reused across every job this call claimed, so an old heartbeat thread
    from a PRIOR claim could in principle still be alive (its `.stop()` not
    yet joined) when a LATER claim happened to be claimed under the exact
    same token, and could then mutate that later claim's row. Minting a
    brand new token via `make_claimant()` for THIS iteration only, BEFORE
    `claim_next(kinds, claimant)` is even called, means every claimed job
    carries its own unique token through mark_running/heartbeat/finish, with
    no token ever shared across two different claims. On an empty queue:
    return `count` immediately if `once`, else sleep `poll_interval` seconds
    and poll again (a real, persistent consumer process).

    The per-job lifecycle itself (mark_running -> heartbeat -> dispatch ->
    finish) is `_run_claimed()`, shared with `run_one()` (the CLI's
    single-job path) -- see its docstring for the mark_running-False skip
    and the dispatch-exception handling.

    `once=True` (used by a future long-running consumer, NOT the CLI --
    see `run_one()` for the CLI's now-single-job path, P1-4/P1-5 fix)
    drains every currently-queued (kind-matching) job, not just one: it
    keeps looping and processing jobs until a `claim_next` call finds the
    queue empty, and ONLY THEN returns.
    """
    count = 0
    while True:
        reap_stale()
        claimant = make_claimant()
        job = claim_next(kinds, claimant)
        if job is None:
            if once:
                return count
            time.sleep(poll_interval)
            continue

        if _run_claimed(job, claimant) is not None:
            count += 1


def run_one(job_id: str) -> Optional[str]:
    """The CLI's single-job path (Task 1.7, P1-4/P1-5 fix): claim EXACTLY
    `job_id` -- never the whole queue -- with a fresh fencing token, then
    run it through the same `_run_claimed()` lifecycle `drain()` uses.

    Used by `worker.run.main()` so a CLI invocation runs ONLY the job it
    just enqueued (or the same-day duplicate it's re-attempting), instead
    of the old `drain(..., once=True)` call which drained the WHOLE queue --
    including every other queued kind (collect/verify/synthesize) this CLI
    has no handler for yet, failing each of them needlessly on every ads
    run.

    Returns the terminal status `_run_claimed` observed ('done'/'failed'),
    or `None` if this call never got to run the job at all: either
    `store.claim_job_by_id` found `job_id` was no longer 'queued' (already
    claimed/running by something else, or already terminal), or the rarer
    race where the lease was revoked between that claim and `mark_running`.
    Either way, `None` means "no authoritative outcome from THIS call" --
    the caller must not infer success or failure from it.
    """
    claimant = make_claimant()
    job = store.claim_job_by_id(job_id, claimant)
    if job is None:
        return None
    return _run_claimed(job, claimant)
