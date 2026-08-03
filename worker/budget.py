"""Per-call spend reservation/settlement over the deployed money-migration
(migration 138: `research_budget_ledger` + the `rs_reserve_spend` /
`rs_settle_call` RPCs), plus per-job cost accrual (Phase 1 tasks 1.4-1.5 of
the deep-research plan).

Three layers guard every paid call, cheapest/fastest-failing first:

  Layer 1 -- `worker.spend_guard.guard()`: a process-local, cross-run rolling
      rate cap persisted to a JSON file. Raises `ResearchSpendCapExceeded`
      BEFORE any network call. Runs unconditionally, for EVERY job --
      project-scoped or ad-hoc.
  Layer 2 -- `reserve()` below, project-scoped jobs only (R3 ad-hoc
      boundary): a DB-enforced worst-case reservation against the project's
      budget ceiling, via the `rs_reserve_spend` RPC. Ad-hoc jobs
      (`job["project_id"]` is None -- the pre-existing ads path) skip this
      layer entirely; they are bounded by Layer 1 + usage reporting only.
  Layer 3 -- `settle()` below: records the TRUE actual cost once the
      provider call returns, and asserts worker-side that `actual_cents`
      never exceeded the reserved worst-case ceiling. A violation is a
      pricing-card bug (`worker.config.Settings.price_cards` under-priced
      this call), not a runtime cap -- the RESERVATION is the real
      guarantee (R2), because by the time `settle()` runs the dollar is
      already spent and nothing here can undo an overspend.

`reserve()`/`settle()` are NOT a lease check on their own. The caller MUST
call `worker.jobs.assert_lease(job_id, claimant)` immediately before the
actual paid call (between `reserve()` returning truthy and invoking the
provider) -- exactly like every other job-spine mutation. `rs_reserve_spend`
also re-checks the lease server-side for project-scoped jobs, but that DB-side
check is not a substitute for the handler's own pre-call fence: an ad-hoc job
has no DB-side lease re-check at all, since it never calls `rs_reserve_spend`.
"""
from dataclasses import dataclass
from typing import Optional

from worker import spend_guard, store
from worker.analyze import usage as usage_reporter
from worker.config import settings


class BudgetOverspendError(Exception):
    """A paid call's TRUE actual cost exceeded its reserved worst-case
    ceiling -- a pricing-card bug (`settings.price_cards` under-priced this
    (job_kind, connector)), not a runtime spend-cap hit. Raised by
    `settle()` BEFORE it touches the ledger, because the reservation -- not
    this assertion -- is the only real guarantee (R2): the dollar was
    already spent by the time `settle()` runs, so no settle-side handling
    can undo an overspend. This must be loud (an exception, not a log line)
    so a mispriced card is caught immediately rather than letting
    `research_budget_ledger`'s committed-spend accounting silently drift out
    of sync with reality."""


@dataclass
class ReserveResult:
    """What `reserve()` hands back: whether the caller may make the paid
    call (`ok`), the worst-case cents that were priced for it
    (`reserved_est_cents` -- `settle()` needs this to assert against), and
    whether this job was project-scoped (`project_scoped` -- False for the
    ad-hoc/R3 path, where no ledger row was ever written).

    Truthy/falsy as a plain bool via `__bool__`, so `if not budget.reserve(
    ...): return` reads exactly like the boolean contract -- while still
    carrying `reserved_est_cents` through to `settle()`."""

    ok: bool
    reserved_est_cents: int
    project_scoped: bool

    def __bool__(self) -> bool:
        return self.ok


def reserve(
    job: dict,
    ref: str,
    claimant: str,
    connector: Optional[str] = None,
) -> ReserveResult:
    """Layer 1 + Layer 2, in order, immediately before a paid call.

    Layer 1 (`spend_guard.guard()`) runs unconditionally, for every job --
    project-scoped or ad-hoc -- and its `ResearchSpendCapExceeded` propagates
    uncaught (it is a `BaseException`, per spend_guard's own contract, so it
    skips `worker.run.main`'s broad `except Exception` and reaches the CLI
    top level).

    Layer 2 runs only when `job["project_id"]` is set (R3 ad-hoc boundary):
    worst-case-prices the call via `settings.price_for(job["job_kind"],
    connector)` and calls the deployed `rs_reserve_spend` RPC. Returns
    `ok=True` when the RPC returned `'ok'` (a fresh reserve -- the caller MAY
    make the paid call); `ok=False` when it returned `'skip'` (this `ref` was
    already reserved -- a replay; the caller must NOT call the provider
    again). Any guard failure inside the RPC (lease not held, project
    unapproved, ceiling exceeded) raises and propagates unchanged -- NOT
    best-effort, matching `worker.store`'s job-spine banner.

    An ad-hoc job (`job["project_id"]` is None) skips the ledger reserve
    entirely and returns `ok=True` -- Layer 1 having already passed is the
    whole gate for this path. `reserved_est_cents` is still populated (from
    the same price card) so `settle()`'s worker-side overspend assertion is
    meaningful even for a job that was never given a DB-enforced ceiling.
    """
    spend_guard.guard()

    est_cents = settings.price_for(job["job_kind"], connector)
    project_id = job.get("project_id")

    if project_id is None:
        # Ad-hoc (R3): no ledger reserve -- spend_guard already passed.
        return ReserveResult(ok=True, reserved_est_cents=est_cents, project_scoped=False)

    outcome = store.rs_reserve_spend(job["id"], project_id, ref, est_cents, claimant, connector)
    if outcome == "ok":
        return ReserveResult(ok=True, reserved_est_cents=est_cents, project_scoped=True)
    if outcome == "skip":
        return ReserveResult(ok=False, reserved_est_cents=est_cents, project_scoped=True)
    raise ValueError(f"rs_reserve_spend returned an unrecognised outcome: {outcome!r}")


def settle(
    job: dict,
    ref: str,
    actual_cents: int,
    claimant: str,
    reserved_est: int,
    report_usage: bool = True,
) -> None:
    """Record the TRUE actual cost of one paid call, once the provider
    response is in hand.

    The worker-side assertion `actual_cents <= reserved_est` runs FIRST,
    before any write: `reserved_est` (from `ReserveResult.reserved_est_cents`,
    itself priced from `settings.price_cards` as a worst-case-from-max-
    tokens ceiling) must never be exceeded by the true cost (R2). A
    violation raises `BudgetOverspendError` loudly -- it is a pricing-card
    bug, not a runtime cap; by the time `settle()` runs the dollar is
    already spent, so this assertion cannot protect the budget itself, only
    surface the bug immediately instead of letting the ledger's committed
    accounting silently drift from reality.

    Project-scoped jobs (`job["project_id"]` set) then call the deployed
    `rs_settle_call` RPC, which is idempotent (a repeat with the same `ref`
    is a no-op server-side) and requires the matching reserve to already
    exist. Ad-hoc jobs (R3) never reserved, so there is nothing to settle in
    the ledger -- this call is skipped for them; the overspend assertion
    above still runs (a pricing-card bug is a bug regardless of ledger
    scope).

    `report_usage=True` (the default) fires one fire-and-forget
    `usage_reporter.cost()` event for every settled call, in dollars
    (`actual_cents / 100`) -- landing this call on the fleet Usage tab. Pass
    `report_usage=False` when the caller already reported this same call at
    a finer grain (e.g. `worker.analyze.analyze()` already calls
    `usage.spend()` with real token counts for its own Anthropic call) so
    the same dollar is never counted twice on the Usage tab.
    """
    if actual_cents > reserved_est:
        raise BudgetOverspendError(
            f"actual_cents={actual_cents} exceeded reserved_est={reserved_est} for "
            f"ref={ref!r} (job_kind={job.get('job_kind')!r}) -- this is a pricing-card "
            "bug (settings.price_cards under-priced this call's worst case), not a "
            "runtime spend cap; the reservation, not this assertion, is the real "
            "guarantee (R2) -- fix the price card, do not adjust this check."
        )

    project_id = job.get("project_id")
    if project_id is not None:
        store.rs_settle_call(ref, actual_cents, claimant)

    if report_usage:
        # `research_jobs` carries no top-level `connector` column (only
        # `job_kind` -- confirmed against the deployed schema); a scrape
        # job's connector, if recorded at all, lives nested under the
        # `params` jsonb column. This is best-effort observability metadata
        # only, never load-bearing: `job.get("params")` may be None (the
        # column is nullable) or omit "connector" entirely, in which case
        # this is simply omitted from the reported event.
        params = job.get("params") or {}
        meta = {"ref": ref}
        connector = params.get("connector") if isinstance(params, dict) else None
        if connector is not None:
            meta["connector"] = connector
        usage_reporter.cost(
            action=f"rs-worker/{job.get('job_kind', 'unknown')}",
            cost=actual_cents / 100,
            store_id=job.get("store_id"),
            meta=meta,
        )


class CostAccrual:
    """A tiny per-job in-memory accumulator: generalizes `worker.analyze`'s
    buffered-usage pattern (buffer as you go, land the total at the end) to
    a job's TOTAL spend rather than a single `UsageReporter` event.

    Usage (by a handler, or the Task 1.7 poll loop that drives it)::

        accrual = CostAccrual()
        ...
        accrual.add(actual_cents)   # once per settled paid call
        ...
        jobs.finish_job(job_id, claimant, "done", cost_cents=accrual.total_cents, ...)

    Deliberately minimal for this task: it only sums in-process. A crash
    mid-job loses any accrued-but-unfinished total (finish_job never runs) --
    this is NOT a regression, since today's uninstrumented `cost_cents` is
    lost the same way on a crash. Persisting a mid-job checkpoint (so a
    crash after call k of N keeps call k's cost even though the job never
    finishes) is explicitly a Task 1.7 (poll loop) concern, not this task's;
    `checkpoint()` below exists as the hook that future work can wire to a
    periodic store write without reworking this class's call sites.
    """

    def __init__(self) -> None:
        self._total_cents = 0
        self._since_checkpoint = 0

    def add(self, cents: int) -> None:
        self._total_cents += cents
        self._since_checkpoint += cents

    @property
    def total_cents(self) -> int:
        return self._total_cents

    def checkpoint(self) -> int:
        """Return the cents accrued since the last `checkpoint()` call and
        reset that counter to zero -- the hook a future periodic-persist
        caller (Task 1.7) can drive without re-summing or double-counting.
        Does not affect `total_cents`, which always holds the job's running
        grand total."""
        pending, self._since_checkpoint = self._since_checkpoint, 0
        return pending
