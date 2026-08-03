"""Tests for worker.run's registry wiring and job-spine integration
(Tasks 1.6/1.7).

The bulk of this file (all tests calling `run.run_ads(...)` below) targets
`run_ads()` -- the extracted, otherwise-UNCHANGED ads orchestration that used
to BE `main()`'s entire body: finish_run must be recorded on EVERY exit path
(guaranteed via `finally`, not dependent on save_analysis succeeding), and
est_claude_calls must reflect whether a Claude call was actually attempted (0
for a fetch_images failure, 1 for a failure past the spend guard) -- see the
P1 fixes this file locks in. These tests were originally written against
`run.main()` before Task 1.6 extracted that body into `run_ads()`; they are
updated here to call `run.run_ads(...)` directly (identical assertions --
`run_ads()` is a pure extract-method refactor of the old `main()`, so nothing
about the orchestration itself changed).

The tests further down (`handle_scrape` / new `main`) cover Task 1.6's
job-spine adapter and Task 1.7's CLI enqueue-and-run_one wiring (P1-4/P1-5
fix: `main` now runs exactly the one job it enqueued via `jobs.run_one`,
never the whole queue via `drain`), which now sit ON TOP of `run_ads()`
rather than being it.
"""
import pytest

from worker import jobs, run, store
from worker.spend_guard import ResearchSpendCapExceeded


class _Recorder:
    """Spies on store.finish_run's call args without hitting the network."""

    def __init__(self):
        self.calls = []

    def __call__(self, run_id, status, scraped_ads=None, analyzed=None, est_claude_calls=0, error=None):
        self.calls.append({
            "run_id": run_id,
            "status": status,
            "scraped_ads": scraped_ads,
            "analyzed": analyzed,
            "est_claude_calls": est_claude_calls,
            "error": error,
        })


@pytest.fixture
def wired(monkeypatch):
    """Common plumbing: start_run returns a fixed id, save_snapshot/get_or_create
    succeed, finish_run is spied on, and ingest is stubbed with two ads."""
    recorder = _Recorder()
    monkeypatch.setattr(store, "get_or_create_competitor", lambda brand, domain=None: "comp-1")
    monkeypatch.setattr(store, "start_run", lambda brand, comp_id=None: "run-1")
    monkeypatch.setattr(store, "finish_run", recorder)
    monkeypatch.setattr(store, "get_pinned_platform_id", lambda comp_id: None)
    monkeypatch.setattr(store, "record_page_identity", lambda *a, **k: None)
    monkeypatch.setattr(store, "save_snapshot", lambda comp_id, platform_id, ads: "snap-1")
    monkeypatch.setattr(store, "save_analysis", lambda *a, **k: "analysis-1")
    monkeypatch.setattr(run.ingest, "resolve_platform_id", lambda brand: "platform-1")
    monkeypatch.setattr(run.ingest, "pull_ads", lambda platform_id: [{"id": "1"}, {"id": "2"}])
    monkeypatch.setattr(run.ingest, "dedup", lambda ads: ads)
    monkeypatch.setattr(run.ingest, "select_for_analysis", lambda ads: ads)
    return recorder


def test_analyze_failure_after_claude_attempted_records_est_1(monkeypatch, wired):
    """A failure inside analyze() happens AFTER the spend guard has already let
    the call through, so it counts as one attempted Claude call (P1-2)."""
    monkeypatch.setattr(run.ingest, "fetch_images", lambda sample, cap=None: [("1", b"x", "image/png")])

    def _boom_analyze(*a, **k):
        raise RuntimeError("stream failed mid-response")

    monkeypatch.setattr(run.analyze, "analyze", _boom_analyze)

    with pytest.raises(RuntimeError):
        run.run_ads("MeUndies")

    assert len(wired.calls) == 1  # finish_run called exactly once
    call = wired.calls[0]
    assert call["run_id"] == "run-1"
    assert call["status"] == "failed"
    assert call["est_claude_calls"] == 1
    assert call["scraped_ads"] == 2
    assert call["analyzed"] == 2
    assert "stream failed" in call["error"]


def test_fetch_images_failure_records_est_0(monkeypatch, wired):
    """A failure in fetch_images happens BEFORE any Claude call, so it must
    never be counted as an attempted call (P1-2)."""

    def _boom_fetch(sample, cap=None):
        raise RuntimeError("image download failed")

    monkeypatch.setattr(run.ingest, "fetch_images", _boom_fetch)
    monkeypatch.setattr(run.analyze, "analyze", lambda *a, **k: pytest.fail("analyze() must not be called"))

    with pytest.raises(RuntimeError):
        run.run_ads("MeUndies")

    assert len(wired.calls) == 1
    call = wired.calls[0]
    assert call["status"] == "failed"
    assert call["est_claude_calls"] == 0
    assert call["scraped_ads"] == 2
    assert call["analyzed"] == 2


def test_finish_run_still_called_when_save_analysis_raises(monkeypatch, wired):
    """P1-1: if store.save_analysis raises inside the except-Exception handler,
    finish_run must still fire (via `finally`) rather than being skipped --
    the old bug left the run row stuck at status='running' forever."""
    monkeypatch.setattr(run.ingest, "fetch_images", lambda sample, cap=None: [("1", b"x", "image/png")])
    monkeypatch.setattr(run.analyze, "analyze", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("analyze boom")))

    def _boom_save_analysis(*a, **k):
        raise RuntimeError("save_analysis boom (e.g. DB down)")

    monkeypatch.setattr(store, "save_analysis", _boom_save_analysis)

    with pytest.raises(RuntimeError, match="save_analysis boom"):
        run.run_ads("MeUndies")

    # finish_run must have run exactly once even though save_analysis raised.
    assert len(wired.calls) == 1
    call = wired.calls[0]
    assert call["status"] == "failed"
    assert call["est_claude_calls"] == 1  # analyze() was attempted before it failed


def test_spend_cap_records_est_0_and_exits_nonzero(monkeypatch, wired):
    """The capped path never calls Claude, so est_claude_calls must be 0, and
    the process must still exit non-zero (sys.exit is preserved through the
    `finally`)."""
    monkeypatch.setattr(run.ingest, "fetch_images", lambda sample, cap=None: [("1", b"x", "image/png")])

    def _boom_cap(*a, **k):
        raise ResearchSpendCapExceeded("hour", count=5, limit=5)

    monkeypatch.setattr(run.analyze, "analyze", _boom_cap)

    with pytest.raises(SystemExit) as exc_info:
        run.run_ads("MeUndies")

    assert exc_info.value.code == 1
    assert len(wired.calls) == 1
    call = wired.calls[0]
    assert call["status"] == "capped"
    assert call["est_claude_calls"] == 0


def test_no_ads_records_finish_run_once(monkeypatch, wired):
    """The early-return no_ads path must still hit finish_run exactly once."""
    monkeypatch.setattr(run.ingest, "pull_ads", lambda platform_id: [])

    run.run_ads("MeUndies")

    assert len(wired.calls) == 1
    call = wired.calls[0]
    assert call["status"] == "no_ads"
    assert call["est_claude_calls"] == 0
    assert call["scraped_ads"] == 0
    assert call["analyzed"] == 0


def test_pinned_platform_id_skips_fuzzy_resolve(monkeypatch, wired):
    """A pinned/observed platform_id in research_page_identities is
    authoritative: when one exists, ingest.resolve_platform_id (the fuzzy
    search that mis-resolved 'Lounge' to an unrelated Chilean retailer) must
    not be called at all, and the pinned id is what gets used downstream."""
    monkeypatch.setattr(store, "get_pinned_platform_id", lambda comp_id: "pinned-platform-1")
    monkeypatch.setattr(
        run.ingest, "resolve_platform_id",
        lambda brand: pytest.fail("resolve_platform_id must not be called when a pin exists"),
    )
    seen_platform_ids = []
    monkeypatch.setattr(
        run.ingest, "pull_ads",
        lambda platform_id: seen_platform_ids.append(platform_id) or [{"id": "1"}],
    )
    monkeypatch.setattr(run.ingest, "fetch_images", lambda sample, cap=None: [])
    monkeypatch.setattr(
        run.analyze, "analyze",
        lambda *a, **k: {"playbook": {}, "winning": [], "proposed_research": []},
    )

    run.run_ads("Lounge")

    assert seen_platform_ids == ["pinned-platform-1"]
    assert wired.calls[0]["status"] == "done"


def test_fuzzy_resolve_records_observed_identity(monkeypatch, wired):
    """When nothing is pinned yet, the existing fuzzy resolve_platform_id path
    runs unchanged, and its result is captured into research_page_identities
    as kind='observed' so the NEXT run for this brand is pinned."""
    monkeypatch.setattr(store, "get_pinned_platform_id", lambda comp_id: None)
    monkeypatch.setattr(run.ingest, "resolve_platform_id", lambda brand: "fuzzy-platform-1")
    recorded = []
    monkeypatch.setattr(store, "record_page_identity", lambda *a, **k: recorded.append((a, k)))
    monkeypatch.setattr(run.ingest, "fetch_images", lambda sample, cap=None: [])
    monkeypatch.setattr(
        run.analyze, "analyze",
        lambda *a, **k: {"playbook": {}, "winning": [], "proposed_research": []},
    )

    run.run_ads("Lounge")

    assert len(recorded) == 1
    args, kwargs = recorded[0]
    assert args == ("comp-1", "fuzzy-platform-1", None, "observed")
    assert kwargs == {}


# ---------------------------------------------------------------------------
# Task 1.6 -- handle_scrape(): the ads-as-scrape job-spine adapter over
# run_ads(). Never touches the real orchestration -- run_ads itself is
# monkeypatched out so these tests are pure adapter-contract tests: what does
# handle_scrape hand back to worker.jobs.drain() for each of run_ads's
# possible exit paths (clean return, sys.exit via the spend cap, any other
# raised exception)?
# ---------------------------------------------------------------------------

def _scrape_job(job_id="job-1", brand="MeUndies", domain="meundies.com"):
    return {
        "id": job_id,
        "job_kind": "scrape",
        "params": {"brand": brand, "domain": domain, "connector": "ad_library.scrapecreators"},
    }


def test_handle_scrape_success_returns_done_none(monkeypatch):
    seen = {}

    def _fake_run_ads(brand, domain=None, job_id=None, claimant=None):
        seen["args"] = (brand, domain, job_id, claimant)

    monkeypatch.setattr(run, "run_ads", _fake_run_ads)

    status, cost_cents, error = run.handle_scrape(_scrape_job(), "claimant-1")

    assert (status, cost_cents, error) == ("done", None, None)
    assert seen["args"] == ("MeUndies", "meundies.com", "job-1", "claimant-1")


def test_handle_scrape_no_ads_clean_return_is_still_done(monkeypatch):
    """run_ads()'s no_ads path is a clean return (no exception) -- a
    successful scrape that found nothing to analyze is not a failure."""
    def _fake_run_ads(brand, domain=None, job_id=None, claimant=None):
        return  # mirrors run_ads's early `return` on the no_ads path

    monkeypatch.setattr(run, "run_ads", _fake_run_ads)

    status, cost_cents, error = run.handle_scrape(_scrape_job(), "claimant-1")

    assert (status, cost_cents, error) == ("done", None, None)


def test_handle_scrape_swallows_spend_cap_sys_exit(monkeypatch):
    """run_ads's own cap-hit branch calls sys.exit(1) -- handle_scrape must
    swallow that SystemExit and fail only this job, not the process, and
    (P2-2 fix) must carry a real error string through rather than
    swallowing it to None."""
    def _fake_run_ads(brand, domain=None, job_id=None, claimant=None):
        raise SystemExit(1)

    monkeypatch.setattr(run, "run_ads", _fake_run_ads)

    status, cost_cents, error = run.handle_scrape(_scrape_job(), "claimant-1")

    assert (status, cost_cents) == ("failed", None)
    assert error is not None and "spend cap" in error.lower()


def test_handle_scrape_swallows_generic_exception(monkeypatch):
    """P2-2 fix: the underlying exception's own message must be preserved
    in the returned error, not swallowed to None -- finish_job's `error`
    column is the only place a non-Claude-call failure's actual message
    survives for later debugging."""
    def _fake_run_ads(brand, domain=None, job_id=None, claimant=None):
        raise RuntimeError("analyze() stream failed")

    monkeypatch.setattr(run, "run_ads", _fake_run_ads)

    status, cost_cents, error = run.handle_scrape(_scrape_job(), "claimant-1")

    assert (status, cost_cents) == ("failed", None)
    assert error == "analyze() stream failed"


def test_handle_scrape_reads_brand_domain_from_params(monkeypatch):
    seen = {}

    def _fake_run_ads(brand, domain=None, job_id=None, claimant=None):
        seen["brand"] = brand
        seen["domain"] = domain

    monkeypatch.setattr(run, "run_ads", _fake_run_ads)

    job = _scrape_job(brand="Lounge", domain=None)
    run.handle_scrape(job, "claimant-1")

    assert seen == {"brand": "Lounge", "domain": None}


def test_handle_scrape_passes_job_id_and_claimant_through(monkeypatch):
    seen = {}

    def _fake_run_ads(brand, domain=None, job_id=None, claimant=None):
        seen["job_id"] = job_id
        seen["claimant"] = claimant

    monkeypatch.setattr(run, "run_ads", _fake_run_ads)

    run.handle_scrape(_scrape_job(job_id="job-42"), "claimant-99")

    assert seen == {"job_id": "job-42", "claimant": "claimant-99"}


# ---------------------------------------------------------------------------
# Task 1.6 -- run_ads()'s own lease-fence addition: jobs.assert_lease() is
# called immediately before the analysis spend ONLY when job_id/claimant are
# both given; it must abort (no analyze() call) when the lease is gone, and
# must not even be consulted for a plain direct call (no job-spine context).
# ---------------------------------------------------------------------------

def test_run_ads_asserts_lease_before_analysis_when_job_context_given(monkeypatch, wired):
    monkeypatch.setattr(run.ingest, "fetch_images", lambda sample, cap=None: [])
    seen = {}

    def _fake_assert_lease(job_id, claimant):
        seen["args"] = (job_id, claimant)
        return True

    monkeypatch.setattr(jobs, "assert_lease", _fake_assert_lease)
    monkeypatch.setattr(
        run.analyze, "analyze",
        lambda *a, **k: {"playbook": {}, "winning": [], "proposed_research": []},
    )

    run.run_ads("MeUndies", job_id="job-1", claimant="claimant-1")

    assert seen["args"] == ("job-1", "claimant-1")
    assert wired.calls[0]["status"] == "done"


def test_run_ads_aborts_analysis_when_lease_lost(monkeypatch, wired):
    monkeypatch.setattr(run.ingest, "fetch_images", lambda sample, cap=None: [])
    monkeypatch.setattr(jobs, "assert_lease", lambda job_id, claimant: False)
    monkeypatch.setattr(
        run.analyze, "analyze",
        lambda *a, **k: pytest.fail("analyze() must not be called once the lease is lost"),
    )

    with pytest.raises(RuntimeError, match="lease lost"):
        run.run_ads("MeUndies", job_id="job-1", claimant="claimant-1")

    assert wired.calls[0]["status"] == "failed"
    assert wired.calls[0]["est_claude_calls"] == 0  # aborted before analyze() ran


def test_run_ads_skips_lease_check_without_job_context(monkeypatch, wired):
    """A direct call (no job_id/claimant -- the pre-job-spine calling
    convention) must not consult jobs.assert_lease at all."""
    monkeypatch.setattr(run.ingest, "fetch_images", lambda sample, cap=None: [])
    monkeypatch.setattr(
        jobs, "assert_lease",
        lambda *a, **k: pytest.fail("assert_lease must not be called without job context"),
    )
    monkeypatch.setattr(
        run.analyze, "analyze",
        lambda *a, **k: {"playbook": {}, "winning": [], "proposed_research": []},
    )

    run.run_ads("MeUndies")

    assert wired.calls[0]["status"] == "done"


def test_run_ads_discards_result_when_lease_lost_after_analysis(monkeypatch, wired):
    """P1-1/P1-2 fix: a SECOND assert_lease check immediately after
    analyze() returns (and before save_analysis) must catch a lease revoked
    DURING the (unavoidable, already-spent) analysis call. The paid call
    already happened -- nothing to do about that -- but the now-stale REAL
    result must never be persisted over whatever the new claimant's own
    re-run has since saved: save_analysis must never be called with the
    real analyzed result / status='ok' once this second fence trips.

    A revoked worker must write NOTHING to research_analyses -- not the real
    result, and not even the except-handler's empty status='failed'
    observability row (which is ALSO lease-gated now): either could still land
    after the new claimant's real analysis and surface as the latest. So when
    the lease is lost, save_analysis is never called at all. (finish_run still
    records this worker's own run row -- that is run-scoped, not the job data
    another claimant now owns.)
    """
    monkeypatch.setattr(run.ingest, "fetch_images", lambda sample, cap=None: [])
    # True pre-spend (Layer 2 gate); False post-analysis (the discard fence
    # raises); False again in the except-handler's own lease gate (skip the
    # observability write too).
    lease_results = iter([True, False, False])
    monkeypatch.setattr(jobs, "assert_lease", lambda job_id, claimant: next(lease_results))
    real_result = {"playbook": {"per_ad": []}, "winning": ["ad-1"], "proposed_research": []}
    monkeypatch.setattr(run.analyze, "analyze", lambda *a, **k: real_result)
    save_calls = []
    monkeypatch.setattr(
        store, "save_analysis",
        lambda *a, **k: save_calls.append((a, k)) or "analysis-1",
    )

    with pytest.raises(RuntimeError, match="lease lost during analysis"):
        run.run_ads("MeUndies", job_id="job-1", claimant="claimant-1")

    # No stale write of ANY kind -- neither the real result nor an empty
    # failed-observability row landed for a job this worker no longer owns.
    assert save_calls == []
    assert wired.calls[0]["status"] == "failed"  # finish_run (run-scoped) still fires
    assert wired.calls[0]["est_claude_calls"] == 1  # analyze() was attempted AND completed


# ---------------------------------------------------------------------------
# Task 1.7 -- main(): pure job-spine plumbing (enqueue one scrape job, reap
# any stale claim, then run ONLY that job via jobs.run_one -- never the
# whole queue, P1-4/P1-5 fix). jobs.enqueue/jobs.reap_stale/jobs.run_one/
# store.find_job_by_idem are all monkeypatched out -- this is a wiring test
# only, proving main() builds the right idempotency key/params, calls
# reap_stale and run_one the right way for both the fresh-enqueue and
# same-day-duplicate paths, and maps run_one's AUTHORITATIVE terminal
# status to the right exit code, never a real claim/dispatch/finish cycle
# (that lives in test_drain.py).
# ---------------------------------------------------------------------------

def test_main_fresh_enqueue_calls_reap_stale_then_run_one_on_its_own_job(monkeypatch, wired):
    from datetime import datetime as real_datetime

    calls = []
    monkeypatch.setattr(run.jobs, "enqueue", lambda *a, **k: calls.append(("enqueue", a, k)) or "job-1")
    monkeypatch.setattr(run.jobs, "reap_stale", lambda: calls.append(("reap_stale",)) or 0)
    monkeypatch.setattr(run.jobs, "run_one", lambda job_id: calls.append(("run_one", job_id)) or "done")
    monkeypatch.setattr(
        run.store, "find_job_by_idem",
        lambda k: pytest.fail("find_job_by_idem must not be called on a fresh enqueue"),
    )

    run.main("MeUndies", "meundies.com")

    # order: enqueue -> reap_stale -> run_one(job_id from THIS enqueue)
    assert [c[0] for c in calls] == ["enqueue", "reap_stale", "run_one"]
    _, args, kwargs = calls[0]
    job_kind, params, idem_key = args
    assert job_kind == "scrape"
    assert params == {
        "brand": "MeUndies",
        "domain": "meundies.com",
        "connector": "ad_library.scrapecreators",
    }
    today = real_datetime.now().strftime("%Y%m%d")
    assert idem_key == f"scrape:ads:adhoc:meundies:{today}"
    assert kwargs == {"project_id": None, "competitor_id": "comp-1"}
    assert calls[2] == ("run_one", "job-1")


def test_main_duplicate_enqueue_still_queued_runs_it_via_run_one(monkeypatch, wired):
    """A same-day duplicate CLI invocation (enqueue returns None) whose
    existing row is still 'queued' (e.g. an earlier invocation died before
    even claiming) must finish it off via run_one, not skip it."""
    monkeypatch.setattr(run.jobs, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(run.jobs, "reap_stale", lambda: 0)
    monkeypatch.setattr(run.store, "find_job_by_idem", lambda k: {"id": "job-existing", "status": "queued"})
    run_one_calls = []
    monkeypatch.setattr(run.jobs, "run_one", lambda job_id: run_one_calls.append(job_id) or "done")

    run.main("MeUndies")

    assert run_one_calls == ["job-existing"]


def test_main_duplicate_enqueue_terminal_existing_uses_its_status_directly(monkeypatch, wired):
    """A same-day duplicate whose existing row is already terminal (done/
    failed/capped/no_ads-via-done) must use that status directly -- run_one
    must NOT be called again for an already-finished job."""
    monkeypatch.setattr(run.jobs, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(run.jobs, "reap_stale", lambda: 0)
    monkeypatch.setattr(run.store, "find_job_by_idem", lambda k: {"id": "job-existing", "status": "done"})
    monkeypatch.setattr(
        run.jobs, "run_one",
        lambda job_id: pytest.fail("run_one must not be called for an already-terminal existing job"),
    )

    run.main("MeUndies")  # done -> exit 0, no SystemExit


def test_main_duplicate_enqueue_terminal_failed_existing_exits_nonzero(monkeypatch, wired):
    """Production exit-code contract on the duplicate path too: an existing
    row already terminal at 'failed' must still exit non-zero."""
    monkeypatch.setattr(run.jobs, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(run.jobs, "reap_stale", lambda: 0)
    monkeypatch.setattr(run.store, "find_job_by_idem", lambda k: {"id": "job-existing", "status": "failed"})
    monkeypatch.setattr(
        run.jobs, "run_one",
        lambda job_id: pytest.fail("run_one must not be called for an already-terminal existing job"),
    )

    with pytest.raises(SystemExit) as excinfo:
        run.main("MeUndies")
    assert excinfo.value.code == 1


def test_main_duplicate_enqueue_claimed_running_existing_is_indeterminate(monkeypatch, wired, caplog):
    """A same-day duplicate whose existing row is 'claimed'/'running' is owned
    by ANOTHER worker right now and may still end 'failed' -- it is NOT a
    terminal success. main() must not run it again (it's not queued) and must
    not treat 'running' as an exit-0 terminal status; it goes down the
    indeterminate (None) path: log a warning, exit 0 (no false failure), and the
    real outcome lands in research_runs via the owning worker."""
    monkeypatch.setattr(run.jobs, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(run.jobs, "reap_stale", lambda: 0)
    monkeypatch.setattr(run.store, "find_job_by_idem", lambda k: {"id": "job-existing", "status": "running"})
    monkeypatch.setattr(
        run.jobs, "run_one",
        lambda job_id: pytest.fail("run_one must not be called for a job another worker owns"),
    )

    with caplog.at_level("WARNING"):
        run.main("MeUndies")  # no SystemExit

    assert any("no authoritative terminal status" in rec.message for rec in caplog.records)


def test_main_duplicate_enqueue_no_existing_row_exits_zero_with_warning(monkeypatch, wired, caplog):
    """The rare edge: enqueue returned None (duplicate) but find_job_by_idem
    can't find/read the row either -- final is None, so main() logs a
    warning and exits 0 rather than assuming failure (the run's real
    outcome is independently recorded in research_runs regardless)."""
    monkeypatch.setattr(run.jobs, "enqueue", lambda *a, **k: None)
    monkeypatch.setattr(run.jobs, "reap_stale", lambda: 0)
    monkeypatch.setattr(run.store, "find_job_by_idem", lambda k: None)
    monkeypatch.setattr(
        run.jobs, "run_one",
        lambda job_id: pytest.fail("run_one must not be called when there is no existing row"),
    )

    with caplog.at_level("WARNING"):
        run.main("MeUndies")  # no SystemExit

    assert any("no authoritative terminal status" in rec.message for rec in caplog.records)


def test_main_exits_nonzero_when_run_one_returns_failed(monkeypatch, wired):
    """Production exit-code contract, fail-CLOSED on run_one's
    AUTHORITATIVE status (not a best-effort re-read, P1-5 fix): 'failed'
    (a raised error or a spend-cap, both mapped to 'failed' by
    handle_scrape/finish_job) must make the CLI exit non-zero, so GitHub
    Actions / a Cloud Run Job / cron sees the run as failed rather than a
    false success."""
    monkeypatch.setattr(run.jobs, "enqueue", lambda *a, **k: "job-1")
    monkeypatch.setattr(run.jobs, "reap_stale", lambda: 0)
    monkeypatch.setattr(run.jobs, "run_one", lambda job_id: "failed")

    with pytest.raises(SystemExit) as excinfo:
        run.main("MeUndies")
    assert excinfo.value.code == 1


def test_main_exits_zero_when_run_one_returns_done(monkeypatch, wired):
    """A clean 'done' scrape (including the no-ads no-op) exits 0 -- main()
    returns normally, raising no SystemExit."""
    monkeypatch.setattr(run.jobs, "enqueue", lambda *a, **k: "job-1")
    monkeypatch.setattr(run.jobs, "reap_stale", lambda: 0)
    monkeypatch.setattr(run.jobs, "run_one", lambda job_id: "done")

    run.main("MeUndies")  # no SystemExit


def test_main_exits_zero_when_run_one_returns_none_with_warning(monkeypatch, wired, caplog):
    """run_one returning None (job_id wasn't 'queued' when this call tried
    to claim it -- e.g. a rare race with another worker) is an
    indeterminate outcome, not a failure: main() must not assume failure,
    only warn and exit 0."""
    monkeypatch.setattr(run.jobs, "enqueue", lambda *a, **k: "job-1")
    monkeypatch.setattr(run.jobs, "reap_stale", lambda: 0)
    monkeypatch.setattr(run.jobs, "run_one", lambda job_id: None)

    with caplog.at_level("WARNING"):
        run.main("MeUndies")  # no SystemExit

    assert any("no authoritative terminal status" in rec.message for rec in caplog.records)


def test_normalize_for_idem_collapses_case_and_whitespace():
    assert run._normalize_for_idem("MeUndies") == "meundies"
    assert run._normalize_for_idem("  Secret Coco  ") == "secret-coco"
    assert run._normalize_for_idem("A  B") == "a-b"
