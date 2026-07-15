"""Tests for the Anthropic spend guard (durable rolling-window call cap)."""
import json

import pytest

from worker.spend_guard import ResearchSpendCapExceeded, guard, snapshot


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "spend_window.json")


def test_under_hourly_cap_is_allowed(monkeypatch, state_file):
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_HOUR", "3")
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_DAY", "1000")
    t = 1_000_000.0

    guard(now=t, state_path=state_file)
    guard(now=t, state_path=state_file)
    # Two calls made, cap is 3 -> still allowed, no raise.
    guard(now=t, state_path=state_file)


def test_at_hourly_cap_raises(monkeypatch, state_file):
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_HOUR", "3")
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_DAY", "1000")
    t = 1_000_000.0

    guard(now=t, state_path=state_file)
    guard(now=t, state_path=state_file)
    guard(now=t, state_path=state_file)

    with pytest.raises(ResearchSpendCapExceeded) as exc:
        guard(now=t, state_path=state_file)
    assert exc.value.window == "hour"
    assert exc.value.limit == 3
    assert exc.value.count == 3


def test_daily_cap_enforced_independently_of_hourly(monkeypatch, state_file):
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_HOUR", "1000")
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_DAY", "3")
    t = 2_000_000.0

    # Spread across the day so the hourly cap never trips.
    guard(now=t, state_path=state_file)
    guard(now=t + 4000, state_path=state_file)
    guard(now=t + 8000, state_path=state_file)

    with pytest.raises(ResearchSpendCapExceeded) as exc:
        guard(now=t + 12000, state_path=state_file)
    assert exc.value.window == "day"
    assert exc.value.limit == 3


def test_entries_older_than_24h_are_pruned(monkeypatch, state_file):
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_HOUR", "1000")
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_DAY", "2")
    t = 3_000_000.0

    guard(now=t, state_path=state_file)
    guard(now=t, state_path=state_file)  # at the day cap now

    # ~25h later the first two are outside the 24h window -> pruned, so allowed.
    guard(now=t + 90_000, state_path=state_file)
    snap = snapshot(now=t + 90_000, state_path=state_file)
    assert snap["calls_last_day"] == 1
    assert snap["calls_last_hour"] == 1


def test_durable_across_separate_process_simulations(monkeypatch, state_file):
    """Two independent guard() invocations against the same state_path must see
    each other's persisted window -- this is the whole point of the file-backed
    guard: `python -m worker.run` is a fresh process every time, so the count
    has to carry across processes via the state file, not module memory."""
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_HOUR", "5")
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_DAY", "1000")
    t = 4_000_000.0

    # "Process 1": one call.
    guard(now=t, state_path=state_file)
    assert snapshot(now=t, state_path=state_file)["calls_last_hour"] == 1

    # "Process 2" (simulated by a fresh call with no shared in-memory state --
    # the module holds no call list itself, only reads/writes the file):
    # two more calls should bring the persisted count to 3.
    guard(now=t + 1, state_path=state_file)
    guard(now=t + 2, state_path=state_file)
    assert snapshot(now=t + 2, state_path=state_file)["calls_last_hour"] == 3

    # "Process 3": drive it to the cap and confirm the block, proving the
    # count really did carry across all three simulated processes.
    guard(now=t + 3, state_path=state_file)
    guard(now=t + 4, state_path=state_file)
    with pytest.raises(ResearchSpendCapExceeded) as exc:
        guard(now=t + 5, state_path=state_file)
    assert exc.value.count == 5


def test_blocked_call_does_not_append(monkeypatch, state_file):
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_HOUR", "2")
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_DAY", "1000")
    t = 5_000_000.0

    guard(now=t, state_path=state_file)
    guard(now=t, state_path=state_file)  # at cap

    for _ in range(3):
        with pytest.raises(ResearchSpendCapExceeded):
            guard(now=t, state_path=state_file)

    # Count must stay at the cap, not grow with each blocked attempt.
    snap = snapshot(now=t, state_path=state_file)
    assert snap["calls_last_hour"] == 2


def test_missing_state_file_treated_as_empty(state_file):
    # state_file path doesn't exist yet -- guard() must not crash.
    snap = snapshot(state_path=state_file)
    assert snap["calls_last_hour"] == 0
    assert snap["calls_last_day"] == 0


def test_corrupt_state_file_treated_as_empty_no_crash(monkeypatch, state_file):
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_HOUR", "20")
    monkeypatch.setenv("RESEARCH_ANALYSIS_MAX_PER_DAY", "80")
    with open(state_file, "w") as f:
        f.write("{not valid json[")

    # Must not raise a JSON/parsing error -- treated as an empty window.
    guard(now=6_000_000.0, state_path=state_file)
    snap = snapshot(now=6_000_000.0, state_path=state_file)
    assert snap["calls_last_hour"] == 1

    # File is now valid JSON again (guard() overwrote it on save).
    with open(state_file) as f:
        data = json.load(f)
    assert data == [6_000_000.0]
