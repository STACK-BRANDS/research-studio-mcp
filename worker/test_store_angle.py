"""Tests for worker.store.save_angle_observations — the migration-173 RPC switch.

Pure logic, no network: worker.store._client is monkeypatched. save_angle_observations must
(1) call the sealed rs_save_angle_observations RPC with just the analysis_id, (2) never raise, and
(3) log at ERROR (not WARNING) when the RPC fails -- after 173's seal the direct write path is gone,
so a failure is a contract breach (angle intel not accruing), not routine noise.
"""
import logging

from worker import store


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _RpcRecorder:
    """A fake supabase client recording the single .rpc(name, params).execute() call, returning a
    preset result (or raising from .execute())."""

    def __init__(self, result=None, raise_exc=None):
        self._result = result
        self._raise = raise_exc
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        recorder = self

        class _Exec:
            def execute(self_inner):
                if recorder._raise is not None:
                    raise recorder._raise
                return recorder._result

        return _Exec()


def test_save_angle_observations_calls_sealed_rpc(monkeypatch):
    rec = _RpcRecorder(result=_FakeResult(3))
    monkeypatch.setattr(store, "_client", lambda: rec)

    store.save_angle_observations("analysis-123")

    # The worker hands the RPC ONLY the analysis_id -- derivation/provenance/grading live in the DB.
    assert rec.calls == [("rs_save_angle_observations", {"p_analysis_id": "analysis-123"})]


def test_save_angle_observations_never_raises_and_logs_error_on_failure(monkeypatch, caplog):
    rec = _RpcRecorder(raise_exc=RuntimeError("boom: provenance failure"))
    monkeypatch.setattr(store, "_client", lambda: rec)

    with caplog.at_level(logging.ERROR, logger=store.logger.name):
        store.save_angle_observations("analysis-err")  # must NOT raise

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a failed RPC must log at ERROR (the loud alert), not WARNING"
    assert any("analysis-err" in r.getMessage() for r in errors)
    assert any("contract breach" in r.getMessage() for r in errors)


def test_save_angle_observations_handles_non_int_data(monkeypatch):
    # A non-int .data (e.g. None / a list) must not raise -- `written` is just reported as None.
    rec = _RpcRecorder(result=_FakeResult(None))
    monkeypatch.setattr(store, "_client", lambda: rec)

    store.save_angle_observations("analysis-nonint")  # must not raise

    assert rec.calls == [("rs_save_angle_observations", {"p_analysis_id": "analysis-nonint"})]
