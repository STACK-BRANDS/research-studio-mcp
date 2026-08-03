"""Tests for the job-spine concurrency core (worker/jobs.py, Phase 1 tasks
1.1-1.3 of the deep-research plan): deterministic project-scoped
idempotency keys (1.1), CAS claim/lease/heartbeat fenced on a per-claim
token (1.2, architecture §13 R1), and the stale-claim reaper (1.3).

No network, no real Supabase: every DB-touching function is exercised
against a fake Supabase client that mimics the query-builder chain
(`.table().update().eq()...execute()`, `.rpc().execute()`) and is
monkeypatched in exactly where the real client is acquired --
`worker.store._client()` -- so the tests prove the actual filter/payload
shape each store function builds, not just its return value.
"""
import os

import pytest

from worker import jobs, store


# ---------------------------------------------------------------------------
# Fake Supabase client -- records every filter/payload built, and returns a
# canned `.data` value configured per test. `bool(res.data)` is how every CAS
# wrapper decides "did a row match" (an empty list == 0 rows affected == the
# lease is not/no-longer held), so `table_response=[]` simulates a revoked
# lease and any non-empty list simulates a held one.
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, table_name, response_data):
        self.table_name = table_name
        self._response_data = response_data
        self.eq_filters = {}
        self.in_filters = {}
        self.lt_filters = {}
        self.update_payload = None
        self.select_cols = None
        self.limit_n = None

    def select(self, cols):
        self.select_cols = cols
        return self

    def update(self, payload):
        self.update_payload = payload
        return self

    def eq(self, col, val):
        self.eq_filters[col] = val
        return self

    def in_(self, col, vals):
        self.in_filters[col] = vals
        return self

    def lt(self, col, val):
        self.lt_filters[col] = val
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def execute(self):
        return _FakeResult(self._response_data)


class _FakeRpc:
    def __init__(self, name, params, response_data):
        self.name = name
        self.params = params
        self._response_data = response_data

    def execute(self):
        return _FakeResult(self._response_data)


class _FakeClient:
    """One shared instance per test, monkeypatched in as `store._client`'s
    return value. `table_response`/`rpc_response` are the canned `.data` to
    hand back from every call; every `.table()`/`.rpc()` invocation is
    recorded (in `queries`/`rpcs`) so a test can assert the exact filters/
    params the store function under test built."""

    def __init__(self, table_response=None, rpc_response=None):
        self.table_response = table_response
        self.rpc_response = rpc_response
        self.queries = []
        self.rpcs = []

    def table(self, name):
        q = _FakeQuery(name, self.table_response)
        self.queries.append(q)
        return q

    def rpc(self, name, params):
        r = _FakeRpc(name, params, self.rpc_response)
        self.rpcs.append(r)
        return r


def _wire(monkeypatch, table_response=None, rpc_response=None):
    client = _FakeClient(table_response=table_response, rpc_response=rpc_response)
    monkeypatch.setattr(store, "_client", lambda: client)
    return client


# ---------------------------------------------------------------------------
# 1.1 -- idempotency-key builders
# ---------------------------------------------------------------------------

def test_idem_scrape_ads_shape():
    assert jobs.idem_scrape_ads("proj-1", "comp-1", "20260803") == "scrape:ads:proj-1:comp-1:20260803"


def test_idem_scrape_web_shape():
    key = jobs.idem_scrape_web("proj-1", "comp-1", "abc123sha", "wave-1")
    assert key == "scrape:web.fetch:proj-1:comp-1:abc123sha:wave-1"


def test_idem_collect_shape():
    assert jobs.idem_collect("proj-1", "capturesha", "v1") == "collect:proj-1:capturesha:v1"


def test_idem_verify_shape():
    assert jobs.idem_verify("proj-1", "scopesha", "v1") == "verify:proj-1:scopesha:v1"


def test_idem_synthesize_shape():
    key = jobs.idem_synthesize("proj-1", "MV", "pdp", "pain_map", "evsha")
    assert key == "synthesize:proj-1:MV:pdp:pain_map:evsha"


def test_with_attempt_appends_suffix():
    base = "scrape:ads:proj-1:comp-1:20260803"
    assert jobs.with_attempt(base, 1) == f"{base}:a1"
    assert jobs.with_attempt(base, 2) == f"{base}:a2"


def test_same_inputs_same_key():
    a = jobs.idem_scrape_ads("proj-1", "comp-1", "20260803")
    b = jobs.idem_scrape_ads("proj-1", "comp-1", "20260803")
    assert a == b


@pytest.mark.parametrize("builder,args_a,args_b", [
    (jobs.idem_scrape_ads, ("proj-1", "comp-1", "20260803"), ("proj-2", "comp-1", "20260803")),
    (jobs.idem_scrape_ads, ("proj-1", "comp-1", "20260803"), ("proj-1", "comp-2", "20260803")),
    (jobs.idem_scrape_ads, ("proj-1", "comp-1", "20260803"), ("proj-1", "comp-1", "20260804")),
    (jobs.idem_scrape_web, ("proj-1", "comp-1", "sha-a", "wave-1"), ("proj-1", "comp-1", "sha-b", "wave-1")),
    (jobs.idem_scrape_web, ("proj-1", "comp-1", "sha-a", "wave-1"), ("proj-1", "comp-1", "sha-a", "wave-2")),
    (jobs.idem_collect, ("proj-1", "sha-a", "v1"), ("proj-1", "sha-a", "v2")),
    (jobs.idem_verify, ("proj-1", "sha-a", "v1"), ("proj-1", "sha-b", "v1")),
    (jobs.idem_synthesize, ("proj-1", "MV", "pdp", "pain_map", "sha-a"),
     ("proj-1", "MV", "plp", "pain_map", "sha-a")),
])
def test_different_inputs_different_keys(builder, args_a, args_b):
    assert builder(*args_a) != builder(*args_b)


def test_project_prefix_prevents_cross_project_collision():
    """R4: two projects scraping the same competitor on the same day must
    NOT collide on the UNIQUE idempotency_key column."""
    key_a = jobs.idem_scrape_ads("proj-1", "comp-1", "20260803")
    key_b = jobs.idem_scrape_ads("proj-2", "comp-1", "20260803")
    assert key_a != key_b


def test_adhoc_project_sentinel_is_a_plain_key_component():
    key = jobs.idem_scrape_ads(jobs.ADHOC_PROJECT, "comp-1", "20260803")
    assert key == "scrape:ads:adhoc:comp-1:20260803"


# ---------------------------------------------------------------------------
# 1.2 -- claim + lease + heartbeat
# ---------------------------------------------------------------------------

def test_make_claimant_shape_and_uniqueness():
    a = jobs.make_claimant()
    b = jobs.make_claimant()
    assert a != b
    assert a.count(":") == 3  # hostname:pid:runid:nonce
    assert f":{os.getpid()}:" in a


def test_claim_next_single_kind_passes_job_kind_and_claimant(monkeypatch):
    client = _wire(monkeypatch, rpc_response=[{"id": "job-1", "job_kind": "scrape"}])
    job = jobs.claim_next(["scrape"], "claimant-1")
    assert job == {"id": "job-1", "job_kind": "scrape"}
    assert len(client.rpcs) == 1
    assert client.rpcs[0].name == "rs_claim_job"
    assert client.rpcs[0].params == {"p_claimed_by": "claimant-1", "p_job_kind": "scrape"}


def test_claim_next_none_kinds_makes_one_call_without_kind_filter(monkeypatch):
    client = _wire(monkeypatch, rpc_response=[{"id": "job-1"}])
    job = jobs.claim_next(None, "claimant-1")
    assert job == {"id": "job-1"}
    assert len(client.rpcs) == 1
    assert client.rpcs[0].params == {"p_claimed_by": "claimant-1"}  # no p_job_kind sent


def test_claim_next_iterates_kinds_until_one_is_queued(monkeypatch):
    """kinds=["collect", "verify"] with nothing queued for "collect" must
    try "verify" next, not give up after the first empty kind."""
    seen_kinds = []

    class _SeqClient(_FakeClient):
        def rpc(self, name, params):
            seen_kinds.append(params.get("p_job_kind"))
            data = [] if params.get("p_job_kind") == "collect" else [{"id": "job-2", "job_kind": "verify"}]
            return _FakeRpc(name, params, data)

    monkeypatch.setattr(store, "_client", lambda: _SeqClient())
    job = jobs.claim_next(["collect", "verify"], "claimant-1")
    assert job == {"id": "job-2", "job_kind": "verify"}
    assert seen_kinds == ["collect", "verify"]


def test_claim_next_returns_none_when_nothing_queued(monkeypatch):
    _wire(monkeypatch, rpc_response=[])
    assert jobs.claim_next(["scrape"], "claimant-1") is None
    assert jobs.claim_next(None, "claimant-1") is None


def test_claim_next_handles_dict_shaped_rpc_response(monkeypatch):
    """Some RPC/client shapes return a single row as a bare dict rather than
    a one-item list -- claim_job must handle both."""
    _wire(monkeypatch, rpc_response={"id": "job-1"})
    assert jobs.claim_next(["scrape"], "claimant-1") == {"id": "job-1"}


def test_claim_next_handles_null_rpc_response(monkeypatch):
    _wire(monkeypatch, rpc_response=None)
    assert jobs.claim_next(["scrape"], "claimant-1") is None


def test_mark_running_true_when_lease_held(monkeypatch):
    client = _wire(monkeypatch, table_response=[{"id": "job-1", "status": "running"}])
    assert jobs.mark_running("job-1", "claimant-1") is True
    q = client.queries[0]
    assert q.table_name == "research_jobs"
    assert q.eq_filters == {"id": "job-1", "claimed_by": "claimant-1", "status": "claimed"}
    assert q.update_payload["status"] == "running"
    assert "started_at" in q.update_payload


def test_mark_running_false_when_lease_revoked(monkeypatch):
    _wire(monkeypatch, table_response=[])  # 0 rows -- claimed_by no longer matches
    assert jobs.mark_running("job-1", "claimant-1") is False


def test_heartbeat_true_when_lease_held(monkeypatch):
    client = _wire(monkeypatch, table_response=[{"id": "job-1"}])
    assert jobs.heartbeat("job-1", "claimant-1") is True
    q = client.queries[0]
    assert q.eq_filters == {"id": "job-1", "claimed_by": "claimant-1", "status": "running"}
    assert "claimed_at" in q.update_payload


def test_heartbeat_false_when_lease_revoked(monkeypatch):
    """A worker whose lease was revoked (reaped mid-run) must have its
    heartbeat return False so it stops instead of pretending it still owns
    the job."""
    _wire(monkeypatch, table_response=[])
    assert jobs.heartbeat("job-1", "claimant-1") is False


def test_assert_lease_true_when_held(monkeypatch):
    client = _wire(monkeypatch, table_response=[{"id": "job-1"}])
    assert jobs.assert_lease("job-1", "claimant-1") is True
    q = client.queries[0]
    assert q.eq_filters == {"id": "job-1", "claimed_by": "claimant-1", "status": "running"}
    assert q.select_cols == "id"
    assert q.limit_n == 1


def test_assert_lease_false_when_revoked(monkeypatch):
    """The pre-side-effect fence a handler must consult before any write or
    spend -- False means abort doing nothing."""
    _wire(monkeypatch, table_response=[])
    assert jobs.assert_lease("job-1", "claimant-1") is False


def test_finish_job_true_when_lease_held_and_writes_all_fields(monkeypatch):
    client = _wire(monkeypatch, table_response=[{"id": "job-1"}])
    ok = jobs.finish_job("job-1", "claimant-1", "done", cost_cents=42, result={"n": 1})
    assert ok is True
    q = client.queries[0]
    assert q.eq_filters == {"id": "job-1", "claimed_by": "claimant-1", "status": "running"}
    assert q.update_payload["status"] == "done"
    assert q.update_payload["cost_cents"] == 42
    assert q.update_payload["result"] == {"n": 1}
    assert "finished_at" in q.update_payload
    assert "error" not in q.update_payload  # not passed -- must not overwrite with a bare null


def test_finish_job_false_when_lease_revoked(monkeypatch):
    """A job whose lease was revoked mid-run must have finish_job return
    False so the caller discards its result rather than writing over
    whatever now owns the row (Sol R1)."""
    _wire(monkeypatch, table_response=[])
    assert jobs.finish_job("job-1", "claimant-1", "done", cost_cents=42) is False


def test_finish_job_failed_status_with_error(monkeypatch):
    client = _wire(monkeypatch, table_response=[{"id": "job-1"}])
    ok = jobs.finish_job("job-1", "claimant-1", "failed", error="boom")
    assert ok is True
    assert client.queries[0].update_payload["error"] == "boom"
    assert client.queries[0].update_payload["status"] == "failed"


# ---------------------------------------------------------------------------
# 1.3 -- stale-claim reaper
# ---------------------------------------------------------------------------

def test_reap_stale_jobs_filter_shape_and_count(monkeypatch):
    client = _wire(monkeypatch, table_response=[{"id": "job-1"}, {"id": "job-2"}])
    count = store.reap_stale_jobs("scrape", 600)
    assert count == 2
    q = client.queries[0]
    assert q.table_name == "research_jobs"
    assert q.in_filters == {"status": ["claimed", "running"]}
    assert q.eq_filters == {"job_kind": "scrape"}
    assert "claimed_at" in q.lt_filters
    assert q.update_payload == {"status": "queued", "claimed_by": None}


def test_reap_stale_jobs_returns_zero_when_nothing_stale(monkeypatch):
    """A live, heartbeating worker's claimed_at stays fresh, so it never
    matches the `lt(claimed_at, cutoff)` filter -- simulated here by an
    empty response."""
    _wire(monkeypatch, table_response=[])
    assert store.reap_stale_jobs("scrape", 600) == 0


def test_reap_stale_iterates_every_configured_kind(monkeypatch):
    """jobs.reap_stale() must sweep every kind in settings.job_lease_timeouts,
    not just the first, and sum the re-queued counts across all of them."""
    from worker.config import settings

    seen = []

    def _fake_reap(kind, timeout_seconds):
        seen.append((kind, timeout_seconds))
        return 1

    monkeypatch.setattr(store, "reap_stale_jobs", _fake_reap)
    total = jobs.reap_stale()
    assert total == len(settings.job_lease_timeouts)
    assert {k for k, _ in seen} == set(settings.job_lease_timeouts.keys())
    # each kind's configured timeout was the one actually passed through
    for kind, timeout in seen:
        assert timeout == settings.job_lease_timeouts[kind]


def test_job_lease_timeouts_covers_all_job_kinds():
    from worker.config import settings

    timeouts = settings.job_lease_timeouts
    assert set(timeouts.keys()) == {"scrape", "collect", "verify", "synthesize"}
    assert all(isinstance(v, int) and v > 0 for v in timeouts.values())


# ---------------------------------------------------------------------------
# 1.7 (P1-4/P1-5 fix) -- store.claim_job_by_id: CAS claim of ONE specific
# job_id (the CLI's "run only the job I just enqueued" path via
# jobs.run_one), never the next-oldest-queued-of-any-kind row claim_job
# picks.
# ---------------------------------------------------------------------------

def test_claim_job_by_id_returns_row_when_still_queued(monkeypatch):
    client = _wire(monkeypatch, table_response=[{"id": "job-1", "status": "claimed", "job_kind": "scrape"}])
    row = store.claim_job_by_id("job-1", "claimant-1")
    assert row == {"id": "job-1", "status": "claimed", "job_kind": "scrape"}
    q = client.queries[0]
    assert q.table_name == "research_jobs"
    assert q.eq_filters == {"id": "job-1", "status": "queued"}
    assert q.update_payload["status"] == "claimed"
    assert q.update_payload["claimed_by"] == "claimant-1"
    assert "claimed_at" in q.update_payload


def test_claim_job_by_id_returns_none_when_not_queued(monkeypatch):
    """Already claimed/running by something else, or already terminal -- 0
    rows matched, the caller must not assume it now owns the job."""
    _wire(monkeypatch, table_response=[])
    assert store.claim_job_by_id("job-1", "claimant-1") is None


def test_claim_job_by_id_handles_dict_shaped_response(monkeypatch):
    """Same dict-vs-list tolerance as claim_job's RPC response handling."""
    _wire(monkeypatch, table_response={"id": "job-1"})
    assert store.claim_job_by_id("job-1", "claimant-1") == {"id": "job-1"}


# ---------------------------------------------------------------------------
# 1.7 (P1-4/P1-5 fix) -- store.find_job_by_idem: best-effort read of
# {id, status} by idempotency_key, used by worker.run.main on a same-day
# duplicate enqueue to find the existing row's id (to run_one it if still
# queued) and status (if already terminal).
# ---------------------------------------------------------------------------

def test_find_job_by_idem_returns_id_and_status(monkeypatch):
    client = _wire(monkeypatch, table_response=[{"id": "job-1", "status": "queued"}])
    result = store.find_job_by_idem("scrape:ads:adhoc:meundies:20260803")
    assert result == {"id": "job-1", "status": "queued"}
    q = client.queries[0]
    assert q.select_cols == "id, status"
    assert q.eq_filters == {"idempotency_key": "scrape:ads:adhoc:meundies:20260803"}
    assert q.limit_n == 1


def test_find_job_by_idem_returns_none_when_absent(monkeypatch):
    _wire(monkeypatch, table_response=[])
    assert store.find_job_by_idem("no-such-key") is None


def test_find_job_by_idem_returns_none_on_exception(monkeypatch):
    """Best-effort, same contract as job_status_for_idem: a read failure
    must never raise, only log and return None."""
    def _boom():
        raise RuntimeError("network blip")
    monkeypatch.setattr(store, "_client", _boom)
    assert store.find_job_by_idem("scrape:ads:adhoc:meundies:20260803") is None
