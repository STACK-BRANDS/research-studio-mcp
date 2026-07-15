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
