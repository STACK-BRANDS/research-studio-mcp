"""Tests for the research_runs registry writers (start_run/finish_run) in worker/store.py.

Pure logic, no network: `worker.store._client` is monkeypatched to something that
raises on every call, proving these two functions are best-effort and NEVER raise
(observability must not break or block the worker) -- see the 2026-07-15 cost
incident this table exists to prevent.
"""
from worker import store


def _raising_client():
    raise RuntimeError("boom: no network / table missing / whatever")


def test_start_run_returns_none_when_client_raises(monkeypatch):
    monkeypatch.setattr(store, "_client", _raising_client)
    assert store.start_run("MeUndies", "some-competitor-id") is None


def test_start_run_returns_none_with_no_competitor_id(monkeypatch):
    monkeypatch.setattr(store, "_client", _raising_client)
    assert store.start_run("MeUndies") is None


def test_finish_run_noop_when_run_id_is_none(monkeypatch):
    monkeypatch.setattr(store, "_client", _raising_client)
    # Should not even attempt to build a client, and must not raise.
    store.finish_run(None, "done", scraped_ads=1, analyzed=1, est_claude_calls=1)


def test_finish_run_never_raises_when_client_raises(monkeypatch):
    monkeypatch.setattr(store, "_client", _raising_client)
    # A real run_id but a broken client -- must swallow the error, not propagate it.
    store.finish_run("some-run-id", "failed", scraped_ads=5, analyzed=2, est_claude_calls=1, error="boom")


def test_finish_run_never_raises_on_capped_status(monkeypatch):
    monkeypatch.setattr(store, "_client", _raising_client)
    store.finish_run("some-run-id", "capped", scraped_ads=None, analyzed=None, est_claude_calls=0, error="spend cap hit")


# --- get_pinned_platform_id / record_page_identity (research_page_identities) ---
#
# Same best-effort contract: a failed read/write must never break the run. A
# minimal fake supabase-py query builder drives the exact chains store.py
# calls (table().select()/insert()...eq()...order()...limit()...execute()).


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, responder):
        self._responder = responder
        self._mode = None
        self._payload = None
        self._filters = {}

    def select(self, *_a, **_k):
        self._mode = "select"
        return self

    def insert(self, payload):
        self._mode = "insert"
        self._payload = payload
        return self

    def eq(self, field, value):
        self._filters[field] = value
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return self._responder(self._mode, self._filters, self._payload)


class _FakeClient:
    def __init__(self, responder):
        self._responder = responder

    def table(self, _name):
        return _FakeQuery(self._responder)


def test_get_pinned_platform_id_prefers_primary_over_more_recent_observed(monkeypatch):
    rows = [
        {"platform_id": "observed-2", "kind": "observed", "first_seen": "2026-08-02T00:00:00Z"},
        {"platform_id": "primary-1", "kind": "primary", "first_seen": "2026-07-01T00:00:00Z"},
    ]
    monkeypatch.setattr(store, "_client", lambda: _FakeClient(lambda mode, filters, payload: _FakeResult(rows)))
    assert store.get_pinned_platform_id("comp-1") == "primary-1"


def test_get_pinned_platform_id_falls_back_to_most_recent_when_no_primary(monkeypatch):
    rows = [{"platform_id": "observed-latest", "kind": "observed", "first_seen": "2026-08-02T00:00:00Z"}]
    monkeypatch.setattr(store, "_client", lambda: _FakeClient(lambda mode, filters, payload: _FakeResult(rows)))
    assert store.get_pinned_platform_id("comp-1") == "observed-latest"


def test_get_pinned_platform_id_returns_none_when_no_rows(monkeypatch):
    monkeypatch.setattr(store, "_client", lambda: _FakeClient(lambda mode, filters, payload: _FakeResult([])))
    assert store.get_pinned_platform_id("comp-1") is None


def test_get_pinned_platform_id_returns_none_when_client_raises(monkeypatch):
    monkeypatch.setattr(store, "_client", _raising_client)
    assert store.get_pinned_platform_id("comp-1") is None


def test_record_page_identity_skips_insert_when_duplicate_exists(monkeypatch):
    calls = []

    def responder(mode, filters, payload):
        calls.append(mode)
        return _FakeResult([{"id": "existing-row"}]) if mode == "select" else _FakeResult([{"id": "new-row"}])

    monkeypatch.setattr(store, "_client", lambda: _FakeClient(responder))
    store.record_page_identity("comp-1", "platform-1", "Some Page", "observed")
    assert calls == ["select"]  # insert never reached


def test_record_page_identity_inserts_when_absent(monkeypatch):
    inserted = []

    def responder(mode, filters, payload):
        if mode == "select":
            return _FakeResult([])
        inserted.append(payload)
        return _FakeResult([{"id": "new-row"}])

    monkeypatch.setattr(store, "_client", lambda: _FakeClient(responder))
    store.record_page_identity("comp-1", "platform-1", "Some Page", "observed")
    assert inserted == [{
        "competitor_id": "comp-1",
        "platform_id": "platform-1",
        "page_name": "Some Page",
        "kind": "observed",
    }]


def test_record_page_identity_never_raises_when_client_raises(monkeypatch):
    monkeypatch.setattr(store, "_client", _raising_client)
    # Must swallow the error, not propagate it -- same contract as start_run/finish_run.
    store.record_page_identity("comp-1", "platform-1", "Some Page", "observed")
