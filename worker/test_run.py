"""Tests for worker.run.main()'s registry wiring: finish_run must be recorded on
EVERY exit path (guaranteed via `finally`, not dependent on save_analysis
succeeding), and est_claude_calls must reflect whether a Claude call was
actually attempted (0 for a fetch_images failure, 1 for a failure past the
spend guard) -- see the P1 fixes this file locks in.
"""
import pytest

from worker import run, store
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
        run.main("MeUndies")

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
        run.main("MeUndies")

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
        run.main("MeUndies")

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
        run.main("MeUndies")

    assert exc_info.value.code == 1
    assert len(wired.calls) == 1
    call = wired.calls[0]
    assert call["status"] == "capped"
    assert call["est_claude_calls"] == 0


def test_no_ads_records_finish_run_once(monkeypatch, wired):
    """The early-return no_ads path must still hit finish_run exactly once."""
    monkeypatch.setattr(run.ingest, "pull_ads", lambda platform_id: [])

    run.main("MeUndies")

    assert len(wired.calls) == 1
    call = wired.calls[0]
    assert call["status"] == "no_ads"
    assert call["est_claude_calls"] == 0
    assert call["scraped_ads"] == 0
    assert call["analyzed"] == 0
