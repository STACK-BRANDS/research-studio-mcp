"""Tests for Task 1.7 of the deep-research plan: `store.enqueue_job` /
`worker.jobs.enqueue` (insert-with-ON-CONFLICT-DO-NOTHING), the
heartbeat-while-running machinery (`worker.jobs._Heartbeat` /
`_spawn_heartbeat`, decision C), the (job_kind, connector) dispatcher
(`worker.jobs._dispatch`), the shared per-job lifecycle helper
(`worker.jobs._run_claimed`), the consumer poll loop itself
(`worker.jobs.drain`), and the CLI's single-job path (`worker.jobs.run_one`,
P1-4/P1-5 fix).

No network, no real Supabase, no real threading-with-real-sleeps in the
orchestration tests: `drain()`'s own tests monkeypatch `reap_stale`,
`make_claimant`, `claim_next`, `mark_running`, `_spawn_heartbeat`,
`_dispatch`, and `finish_job` directly, so a `drain()` test never spins a
real thread and never touches a real store call -- it proves only the
loop's OWN orchestration (call order, a FRESH claimant token minted per
iteration -- P1-3 fix, skip-on-False, heartbeat start/stop symmetry, count
semantics). The heartbeat tick logic (`_Heartbeat._loop`) is tested
separately, deterministically, by calling `_loop()` directly with an
injected fake `wait_fn` (no real `time.sleep`, no real `threading.Event`
wait) -- only one small smoke test actually starts a real thread, and it
uses a tiny (sub-10ms) interval so it never meaningfully sleeps either.
"""
import threading

import pytest

from worker import jobs, store


# ---------------------------------------------------------------------------
# Fake Supabase client -- same shape as test_jobs.py's / test_budget.py's
# fakes, extended with `.upsert()` for `store.enqueue_job`.
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, table_name, response_data):
        self.table_name = table_name
        self._response_data = response_data
        self.upsert_payload = None
        self.upsert_on_conflict = None
        self.upsert_ignore_duplicates = None

    def upsert(self, payload, on_conflict=None, ignore_duplicates=False, **kwargs):
        self.upsert_payload = payload
        self.upsert_on_conflict = on_conflict
        self.upsert_ignore_duplicates = ignore_duplicates
        return self

    def execute(self):
        return _FakeResult(self._response_data)


class _FakeClient:
    def __init__(self, table_response=None):
        self.table_response = table_response
        self.queries = []

    def table(self, name):
        q = _FakeQuery(name, self.table_response)
        self.queries.append(q)
        return q


def _wire(monkeypatch, table_response=None):
    client = _FakeClient(table_response=table_response)
    monkeypatch.setattr(store, "_client", lambda: client)
    return client


# ---------------------------------------------------------------------------
# store.enqueue_job
# ---------------------------------------------------------------------------

def test_enqueue_job_inserts_and_returns_id(monkeypatch):
    client = _wire(monkeypatch, table_response=[{"id": "job-1"}])

    result = store.enqueue_job(
        "scrape", {"brand": "MeUndies", "connector": "ad_library.scrapecreators"},
        "scrape:ads:adhoc:meundies:20260803",
    )

    assert result == "job-1"
    q = client.queries[0]
    assert q.table_name == "research_jobs"
    assert q.upsert_on_conflict == "idempotency_key"
    assert q.upsert_ignore_duplicates is True
    assert q.upsert_payload == {
        "job_kind": "scrape",
        "params": {"brand": "MeUndies", "connector": "ad_library.scrapecreators"},
        "idempotency_key": "scrape:ads:adhoc:meundies:20260803",
    }
    # status is never sent -- the column's own DB default ('queued') applies.
    assert "status" not in q.upsert_payload


def test_enqueue_job_conflict_returns_none(monkeypatch):
    """A conflicting idempotency_key is never returned by PostgREST under
    ignore_duplicates=True (ON CONFLICT DO NOTHING) -- empty .data IS the
    duplicate signal."""
    _wire(monkeypatch, table_response=[])

    result = store.enqueue_job("scrape", {}, "scrape:ads:adhoc:meundies:20260803")

    assert result is None


def test_enqueue_job_includes_optional_fields_when_given(monkeypatch):
    client = _wire(monkeypatch, table_response=[{"id": "job-1"}])

    store.enqueue_job(
        "collect", {"x": 1}, "collect:proj-1:sha:v1",
        project_id="proj-1", competitor_id="comp-1", store_id="store-1",
    )

    payload = client.queries[0].upsert_payload
    assert payload["project_id"] == "proj-1"
    assert payload["competitor_id"] == "comp-1"
    assert payload["store_id"] == "store-1"


def test_enqueue_job_omits_none_optional_fields(monkeypatch):
    client = _wire(monkeypatch, table_response=[{"id": "job-1"}])

    store.enqueue_job("scrape", {}, "scrape:ads:adhoc:meundies:20260803")

    payload = client.queries[0].upsert_payload
    assert "project_id" not in payload
    assert "competitor_id" not in payload
    assert "store_id" not in payload


# ---------------------------------------------------------------------------
# jobs.enqueue -- thin wrapper over store.enqueue_job
# ---------------------------------------------------------------------------

def test_jobs_enqueue_passes_through_all_args(monkeypatch):
    seen = {}

    def _fake_enqueue_job(job_kind, params, idempotency_key, project_id=None,
                           competitor_id=None, store_id=None):
        seen.update(job_kind=job_kind, params=params, idempotency_key=idempotency_key,
                    project_id=project_id, competitor_id=competitor_id, store_id=store_id)
        return "job-1"

    monkeypatch.setattr(store, "enqueue_job", _fake_enqueue_job)

    result = jobs.enqueue(
        "scrape", {"brand": "MeUndies"}, "scrape:ads:adhoc:meundies:20260803",
        project_id=None, competitor_id="comp-1",
    )

    assert result == "job-1"
    assert seen == {
        "job_kind": "scrape",
        "params": {"brand": "MeUndies"},
        "idempotency_key": "scrape:ads:adhoc:meundies:20260803",
        "project_id": None,
        "competitor_id": "comp-1",
        "store_id": None,
    }


def test_jobs_enqueue_returns_none_on_duplicate(monkeypatch):
    monkeypatch.setattr(store, "enqueue_job", lambda *a, **k: None)
    assert jobs.enqueue("scrape", {}, "dup-key") is None


# ---------------------------------------------------------------------------
# jobs._Heartbeat -- the tick logic, tested deterministically via a fake
# wait_fn (no real time.sleep, no real threading.Event timeout).
# ---------------------------------------------------------------------------

def test_heartbeat_loop_ticks_until_wait_fn_signals_stop():
    """A fake wait_fn returns False (keep ticking) three times, then True
    (stop signal received) -- heartbeat_fn must be called exactly 3 times."""
    calls = []
    wait_returns = iter([False, False, False, True])

    def _fake_wait(interval):
        return next(wait_returns)

    def _fake_heartbeat(job_id, claimant):
        calls.append((job_id, claimant))
        return True

    hb = jobs._Heartbeat("job-1", "claimant-1", interval=30, heartbeat_fn=_fake_heartbeat,
                          wait_fn=_fake_wait)
    hb._loop()  # call directly -- no real thread, no real sleep

    assert calls == [("job-1", "claimant-1")] * 3


def test_heartbeat_loop_stops_when_heartbeat_fn_returns_false():
    """Even if wait_fn never signals stop, a heartbeat_fn False (lease
    revoked) must break the loop -- otherwise a revoked lease would spin
    forever calling heartbeat on a job that's no longer this claimant's."""
    calls = []

    def _fake_wait(interval):
        return False  # never signals stop on its own

    def _fake_heartbeat(job_id, claimant):
        calls.append(1)
        return len(calls) < 2  # True on call 1, False on call 2

    hb = jobs._Heartbeat("job-1", "claimant-1", interval=30, heartbeat_fn=_fake_heartbeat,
                          wait_fn=_fake_wait)
    hb._loop()

    assert len(calls) == 2  # stopped right after the False, not spinning forever


def test_heartbeat_loop_stops_on_heartbeat_fn_exception():
    """A heartbeat_fn that raises must not propagate out of the (background,
    in production) loop -- it logs and stops, same as a False return."""
    calls = []

    def _fake_wait(interval):
        return False

    def _fake_heartbeat(job_id, claimant):
        calls.append(1)
        raise RuntimeError("network blip")

    hb = jobs._Heartbeat("job-1", "claimant-1", interval=30, heartbeat_fn=_fake_heartbeat,
                          wait_fn=_fake_wait)
    hb._loop()  # must not raise

    assert len(calls) == 1


def test_heartbeat_real_thread_smoke():
    """A minimal, bounded smoke test that .start()/.stop() actually wire up
    a real background thread -- uses a tiny (10ms) interval so this never
    meaningfully sleeps; the deterministic tick-logic tests above are the
    real coverage."""
    called = threading.Event()

    def _fake_heartbeat(job_id, claimant):
        called.set()
        return True

    hb = jobs._Heartbeat("job-1", "claimant-1", interval=0.01, heartbeat_fn=_fake_heartbeat)
    hb.start()
    try:
        assert called.wait(timeout=1.0)  # heartbeat_fn ran at least once
    finally:
        hb.stop(join_timeout=1.0)


def test_spawn_heartbeat_uses_min_timeout_over_3_and_30(monkeypatch):
    captured = {}

    class _FakeHeartbeat:
        def __init__(self, job_id, claimant, interval, heartbeat_fn=None, wait_fn=None):
            captured["args"] = (job_id, claimant, interval)

        def start(self):
            return self

    monkeypatch.setattr(jobs, "_Heartbeat", _FakeHeartbeat)
    monkeypatch.setenv("RESEARCH_JOB_LEASE_TIMEOUT_COLLECT", "60")

    jobs._spawn_heartbeat("job-1", "claimant-1", "collect")

    assert captured["args"] == ("job-1", "claimant-1", 20)  # min(60 // 3, 30) == 20


def test_spawn_heartbeat_caps_interval_at_30(monkeypatch):
    captured = {}

    class _FakeHeartbeat:
        def __init__(self, job_id, claimant, interval, heartbeat_fn=None, wait_fn=None):
            captured["args"] = (job_id, claimant, interval)

        def start(self):
            return self

    monkeypatch.setattr(jobs, "_Heartbeat", _FakeHeartbeat)

    jobs._spawn_heartbeat("job-1", "claimant-1", "scrape")  # default timeout 600

    assert captured["args"] == ("job-1", "claimant-1", 30)  # min(600 // 3, 30) == 30


def test_spawn_heartbeat_falls_back_for_unknown_job_kind(monkeypatch):
    captured = {}

    class _FakeHeartbeat:
        def __init__(self, job_id, claimant, interval, heartbeat_fn=None, wait_fn=None):
            captured["args"] = (job_id, claimant, interval)

        def start(self):
            return self

    monkeypatch.setattr(jobs, "_Heartbeat", _FakeHeartbeat)

    jobs._spawn_heartbeat("job-1", "claimant-1", "no-such-kind")

    assert captured["args"] == ("job-1", "claimant-1", 30)  # min(300 // 3, 30) fallback == 30


# ---------------------------------------------------------------------------
# jobs._dispatch -- routes by (job_kind, params.connector)
# ---------------------------------------------------------------------------

def test_dispatch_routes_scrape_ad_library_scrapecreators_to_handle_scrape(monkeypatch):
    from worker import run

    seen = {}

    def _fake_handle_scrape(job, claimant):
        seen["args"] = (job, claimant)
        return "done", 42, None

    monkeypatch.setattr(run, "handle_scrape", _fake_handle_scrape)

    job = {"id": "job-1", "job_kind": "scrape",
           "params": {"connector": "ad_library.scrapecreators"}}
    result = jobs._dispatch(job, "claimant-1")

    assert result == ("done", 42, None)
    assert seen["args"] == (job, "claimant-1")


def test_dispatch_passes_through_scrape_handler_error(monkeypatch):
    """P2-2 fix: handle_scrape now returns a real error string (not always
    None) on a failure -- _dispatch must pass it through to finish_job
    unchanged, not drop it."""
    from worker import run

    monkeypatch.setattr(
        run, "handle_scrape",
        lambda job, claimant: ("failed", None, "spend cap hit (exit code 1)"),
    )

    job = {"id": "job-1", "job_kind": "scrape",
           "params": {"connector": "ad_library.scrapecreators"}}
    result = jobs._dispatch(job, "claimant-1")

    assert result == ("failed", None, "spend cap hit (exit code 1)")


def test_dispatch_unknown_connector_fails_without_calling_any_handler(monkeypatch):
    from worker import run
    monkeypatch.setattr(
        run, "handle_scrape",
        lambda *a, **k: pytest.fail("handle_scrape must not be called for an unmapped connector"),
    )

    job = {"id": "job-1", "job_kind": "scrape", "params": {"connector": "web.fetch"}}
    result = jobs._dispatch(job, "claimant-1")

    assert result == ("failed", None, "handler not implemented (P2-P4)")


def test_dispatch_unimplemented_job_kind_fails_cleanly():
    job = {"id": "job-1", "job_kind": "collect", "params": {}}
    result = jobs._dispatch(job, "claimant-1")
    assert result == ("failed", None, "handler not implemented (P2-P4)")


def test_dispatch_handles_missing_params():
    job = {"id": "job-1", "job_kind": "verify", "params": None}
    result = jobs._dispatch(job, "claimant-1")
    assert result == ("failed", None, "handler not implemented (P2-P4)")


# ---------------------------------------------------------------------------
# jobs.drain / jobs._run_claimed / jobs.run_one -- every collaborator is
# monkeypatched: this proves ONLY the orchestration itself (call order, a
# FRESH claimant token minted per claim -- P1-3 fix, the mark_running=False
# skip, heartbeat start/stop symmetry including on a handler exception, and
# the once=True "drain until empty" semantics), never a real claim/dispatch/
# finish cycle against a real store.
# ---------------------------------------------------------------------------

class _FakeHeartbeatHandle:
    """Records start/stop without ever touching a real thread."""

    def __init__(self, job_id, claimant, job_kind, registry):
        self.job_id = job_id
        self.claimant = claimant
        self.job_kind = job_kind
        self.stopped = False
        registry.append(self)

    def stop(self, join_timeout=5.0):
        self.stopped = True


@pytest.fixture
def drain_stubs(monkeypatch):
    """Wires every drain()/_run_claimed()/run_one() collaborator to a
    recording fake, with a configurable jobs queue. `make_claimant` is
    stubbed to mint deterministic, sequential, and DISTINCT tokens
    ("claimant-1", "claimant-2", ...) on every call -- P1-3 fix: proves a
    fresh token is minted per claim rather than one token being reused
    across a whole drain() invocation. Returns a dict of call-log lists."""
    log = {
        "reap_stale": [],
        "make_claimant": [],
        "claim_next": [],
        "mark_running": [],
        "spawn_heartbeat": [],
        "dispatch": [],
        "finish_job": [],
    }
    heartbeats = []
    state = {"queue": [], "mark_running_result": True, "dispatch_result": ("done", 10, None),
              "dispatch_raises": None, "finish_job_result": True}

    def _reap_stale():
        log["reap_stale"].append(1)
        return 0

    def _make_claimant():
        token = f"claimant-{len(log['make_claimant']) + 1}"
        log["make_claimant"].append(token)
        return token

    def _claim_next(kinds, claimant):
        log["claim_next"].append((kinds, claimant))
        if state["queue"]:
            return state["queue"].pop(0)
        return None

    def _mark_running(job_id, claimant):
        log["mark_running"].append((job_id, claimant))
        return state["mark_running_result"]

    def _spawn_heartbeat(job_id, claimant, job_kind):
        log["spawn_heartbeat"].append((job_id, claimant, job_kind))
        return _FakeHeartbeatHandle(job_id, claimant, job_kind, heartbeats)

    def _dispatch(job, claimant):
        log["dispatch"].append((job, claimant))
        if state["dispatch_raises"] is not None:
            raise state["dispatch_raises"]
        return state["dispatch_result"]

    def _finish_job(job_id, claimant, status, error=None, cost_cents=None, result=None):
        log["finish_job"].append({
            "job_id": job_id, "claimant": claimant, "status": status,
            "error": error, "cost_cents": cost_cents,
        })
        return state["finish_job_result"]

    monkeypatch.setattr(jobs, "reap_stale", _reap_stale)
    monkeypatch.setattr(jobs, "make_claimant", _make_claimant)
    monkeypatch.setattr(jobs, "claim_next", _claim_next)
    monkeypatch.setattr(jobs, "mark_running", _mark_running)
    monkeypatch.setattr(jobs, "_spawn_heartbeat", _spawn_heartbeat)
    monkeypatch.setattr(jobs, "_dispatch", _dispatch)
    monkeypatch.setattr(jobs, "finish_job", _finish_job)

    log["heartbeats"] = heartbeats
    log["state"] = state
    return log


def test_drain_once_processes_until_empty_then_returns_count(drain_stubs):
    drain_stubs["state"]["queue"] = [
        {"id": "job-1", "job_kind": "scrape", "params": {}},
        {"id": "job-2", "job_kind": "scrape", "params": {}},
    ]

    count = jobs.drain(once=True)

    assert count == 2
    assert len(drain_stubs["reap_stale"]) == 3  # once per iteration, including the final empty one
    assert [c[0] for c in drain_stubs["mark_running"]] == ["job-1", "job-2"]
    # P1-3 fix: each claimed job gets its OWN fresh claimant token, never a
    # token shared across two different claims.
    claimants = [c[1] for c in drain_stubs["mark_running"]]
    assert claimants[0] != claimants[1]
    assert [f["job_id"] for f in drain_stubs["finish_job"]] == ["job-1", "job-2"]
    assert all(f["status"] == "done" and f["cost_cents"] == 10 for f in drain_stubs["finish_job"])
    # ... but the SAME token flows through mark_running -> finish_job for
    # any ONE given job's lifecycle.
    assert [f["claimant"] for f in drain_stubs["finish_job"]] == claimants
    assert all(hb.stopped for hb in drain_stubs["heartbeats"])


def test_drain_mints_fresh_claimant_before_each_claim_next_call(drain_stubs):
    """P1-3 fix, the direct test: make_claimant() must be called BEFORE
    claim_next() on every loop iteration (including the trailing empty
    poll), with each iteration's freshly-minted token flowing through that
    iteration's entire per-job lifecycle -- never the whole-call-reused
    token the old drain(claimant, ...) signature allowed."""
    drain_stubs["state"]["queue"] = [
        {"id": "job-1", "job_kind": "scrape", "params": {}},
        {"id": "job-2", "job_kind": "scrape", "params": {}},
    ]

    jobs.drain(once=True)

    # one fresh claimant per loop iteration: 2 claimed jobs + the final
    # empty-queue poll that ends the once=True loop.
    assert len(drain_stubs["make_claimant"]) == 3
    claimants = drain_stubs["make_claimant"]
    assert len(set(claimants)) == 3  # every mint is unique

    # claim_next receives THIS iteration's freshly-minted token, not a
    # carried-over one.
    assert [c[1] for c in drain_stubs["claim_next"]] == claimants

    # and that exact token flows through the whole per-job lifecycle.
    assert drain_stubs["mark_running"][0] == ("job-1", claimants[0])
    assert drain_stubs["mark_running"][1] == ("job-2", claimants[1])
    assert drain_stubs["spawn_heartbeat"][0][1] == claimants[0]
    assert drain_stubs["spawn_heartbeat"][1][1] == claimants[1]
    assert drain_stubs["finish_job"][0]["claimant"] == claimants[0]
    assert drain_stubs["finish_job"][1]["claimant"] == claimants[1]


def test_drain_mark_running_false_skips_job_without_heartbeat_or_finish(drain_stubs):
    drain_stubs["state"]["queue"] = [{"id": "job-1", "job_kind": "scrape", "params": {}}]
    drain_stubs["state"]["mark_running_result"] = False

    count = jobs.drain(once=True)

    assert count == 0
    assert drain_stubs["spawn_heartbeat"] == []
    assert drain_stubs["dispatch"] == []
    assert drain_stubs["finish_job"] == []


def test_drain_dispatch_exception_finishes_job_failed_not_crash(drain_stubs):
    drain_stubs["state"]["queue"] = [{"id": "job-1", "job_kind": "scrape", "params": {}}]
    drain_stubs["state"]["dispatch_raises"] = RuntimeError("handler blew up")

    count = jobs.drain(once=True)

    assert count == 1  # the job WAS processed, just unsuccessfully
    finish = drain_stubs["finish_job"][0]
    assert finish["status"] == "failed"
    assert finish["cost_cents"] is None
    assert "handler blew up" in finish["error"]
    assert drain_stubs["heartbeats"][0].stopped is True  # finally still ran


def test_drain_uses_job_kind_for_heartbeat_spawn(drain_stubs):
    drain_stubs["state"]["queue"] = [{"id": "job-1", "job_kind": "verify", "params": {}}]

    jobs.drain(once=True)

    assert drain_stubs["spawn_heartbeat"] == [("job-1", "claimant-1", "verify")]


def test_drain_dispatch_failed_status_passes_through_error(drain_stubs):
    drain_stubs["state"]["queue"] = [{"id": "job-1", "job_kind": "collect", "params": {}}]
    drain_stubs["state"]["dispatch_result"] = ("failed", None, "handler not implemented (P2-P4)")

    jobs.drain(once=True)

    finish = drain_stubs["finish_job"][0]
    assert finish["status"] == "failed"
    assert finish["error"] == "handler not implemented (P2-P4)"
    assert finish["cost_cents"] is None


def test_drain_not_once_sleeps_then_continues_on_empty_queue(drain_stubs, monkeypatch):
    """The persistent (once=False) path must sleep poll_interval and keep
    polling on an empty queue -- proven here without ever really sleeping by
    raising a sentinel from the second sleep call to escape the infinite
    loop deterministically."""
    sleep_calls = []

    class _StopLoop(Exception):
        pass

    def _fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise _StopLoop()

    monkeypatch.setattr(jobs.time, "sleep", _fake_sleep)

    with pytest.raises(_StopLoop):
        jobs.drain(once=False, poll_interval=2.0)

    assert sleep_calls == [2.0, 2.0]
    assert len(drain_stubs["claim_next"]) == 2


def test_drain_returns_zero_when_queue_starts_empty(drain_stubs):
    count = jobs.drain(once=True)
    assert count == 0
    assert drain_stubs["finish_job"] == []


def test_drain_passes_kinds_through_to_claim_next(drain_stubs):
    jobs.drain(kinds=["scrape", "collect"], once=True)
    assert drain_stubs["claim_next"][0] == (["scrape", "collect"], "claimant-1")


# ---------------------------------------------------------------------------
# jobs._run_claimed -- the per-job lifecycle helper (mark_running -> spawn
# heartbeat -> dispatch (try/finally) -> finish_job), extracted (P1-3/P1-4
# fix) so drain() and run_one() share ONE lifecycle implementation instead
# of one reimplementing what the other already does. Tested directly here
# (bypassing reap_stale/make_claimant/claim_next entirely) using the same
# drain_stubs fixture.
# ---------------------------------------------------------------------------

def test_run_claimed_returns_none_when_mark_running_fails(drain_stubs):
    drain_stubs["state"]["mark_running_result"] = False
    job = {"id": "job-1", "job_kind": "scrape", "params": {}}

    result = jobs._run_claimed(job, "claimant-1")

    assert result is None
    assert drain_stubs["spawn_heartbeat"] == []
    assert drain_stubs["dispatch"] == []
    assert drain_stubs["finish_job"] == []


def test_run_claimed_returns_terminal_status_on_success(drain_stubs):
    job = {"id": "job-1", "job_kind": "scrape", "params": {}}

    result = jobs._run_claimed(job, "claimant-1")

    assert result == "done"
    assert drain_stubs["mark_running"] == [("job-1", "claimant-1")]
    assert drain_stubs["finish_job"][0]["status"] == "done"
    assert drain_stubs["finish_job"][0]["claimant"] == "claimant-1"
    assert drain_stubs["heartbeats"][0].stopped is True


def test_run_claimed_returns_failed_status_and_stops_heartbeat_on_exception(drain_stubs):
    drain_stubs["state"]["dispatch_raises"] = RuntimeError("handler blew up")
    job = {"id": "job-1", "job_kind": "scrape", "params": {}}

    result = jobs._run_claimed(job, "claimant-1")

    assert result == "failed"
    assert "handler blew up" in drain_stubs["finish_job"][0]["error"]
    assert drain_stubs["heartbeats"][0].stopped is True


def test_run_claimed_returns_none_when_finish_job_cas_fails(drain_stubs):
    """The lease was revoked after dispatch but before finish_job: its CAS
    matches 0 rows and returns False. The outcome was NOT persisted (the reaper
    already re-queued the job for a new claimant), so _run_claimed must return
    None -- never a terminal status the caller would treat as authoritative
    (a 'done' here would wrongly exit 0 for a job that may still fail)."""
    drain_stubs["state"]["finish_job_result"] = False
    job = {"id": "job-1", "job_kind": "scrape", "params": {}}

    result = jobs._run_claimed(job, "claimant-1")

    assert result is None
    # It still ATTEMPTED the finish (the CAS is how it learns it lost the lease)
    # and still stopped its heartbeat.
    assert drain_stubs["finish_job"][0]["status"] == "done"
    assert drain_stubs["heartbeats"][0].stopped is True


# ---------------------------------------------------------------------------
# jobs.run_one -- the CLI's single-job path (P1-4/P1-5 fix): claim EXACTLY
# one job_id via store.claim_job_by_id with a fresh fencing token, then run
# it through the SAME _run_claimed lifecycle drain() uses. Never touches
# claim_next/the whole queue.
# ---------------------------------------------------------------------------

def test_run_one_claims_by_id_with_fresh_claimant_and_runs_it(monkeypatch, drain_stubs):
    seen = {}

    def _fake_make_claimant():
        seen["claimant"] = "fresh-claimant-1"
        return seen["claimant"]

    def _fake_claim_job_by_id(job_id, claimant):
        seen["claim_args"] = (job_id, claimant)
        return {"id": job_id, "job_kind": "scrape", "params": {}}

    monkeypatch.setattr(jobs, "make_claimant", _fake_make_claimant)
    monkeypatch.setattr(store, "claim_job_by_id", _fake_claim_job_by_id)

    result = jobs.run_one("job-1")

    assert result == "done"
    assert seen["claim_args"] == ("job-1", "fresh-claimant-1")
    assert drain_stubs["mark_running"][0] == ("job-1", "fresh-claimant-1")
    assert drain_stubs["finish_job"][0]["status"] == "done"


def test_run_one_returns_none_when_job_not_queued(monkeypatch, drain_stubs):
    """store.claim_job_by_id returning None (the job wasn't 'queued' --
    already claimed/running by something else, or already terminal) must
    surface as None, and must not touch mark_running/dispatch/finish at
    all."""
    monkeypatch.setattr(jobs, "make_claimant", lambda: "fresh-claimant-1")
    monkeypatch.setattr(store, "claim_job_by_id", lambda job_id, claimant: None)

    result = jobs.run_one("job-1")

    assert result is None
    assert drain_stubs["mark_running"] == []
    assert drain_stubs["spawn_heartbeat"] == []
    assert drain_stubs["dispatch"] == []
    assert drain_stubs["finish_job"] == []


def test_run_one_returns_none_when_mark_running_fails_after_claim(monkeypatch, drain_stubs):
    """A rare race: claim_job_by_id succeeds but the lease is revoked before
    mark_running -- still surfaces as None (indeterminate), not a crash."""
    monkeypatch.setattr(jobs, "make_claimant", lambda: "fresh-claimant-1")
    monkeypatch.setattr(
        store, "claim_job_by_id",
        lambda job_id, claimant: {"id": job_id, "job_kind": "scrape", "params": {}},
    )
    drain_stubs["state"]["mark_running_result"] = False

    result = jobs.run_one("job-1")

    assert result is None
