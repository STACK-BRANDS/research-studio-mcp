"""Anthropic spend guard: a rolling-window rate cap, persisted across processes.

`worker.analyze.analyze()` calls `guard()` immediately before the single
`client.messages.stream(...)` call in this repo. `guard()` counts calls in
rolling 1-hour and 24-hour windows and raises `ResearchSpendCapExceeded` --
BEFORE the API call, so no tokens (and no spend) happen past the cap -- once a
hard limit is crossed.

Unlike a long-lived bot process, `python -m worker.run "<brand>"` is a fresh
CLI invocation every time: a pure in-memory window would reset on every run
and never catch cross-run abuse. This is a direct response to a real incident
(2026-07-15): 13 manual CLI re-runs in one morning each re-billed a full
uncached ~$0.50 claude-sonnet-5 call, because there was no cache and no rate
cap around the call site. So the window here is PERSISTED to a small JSON
state file (a flat list of unix-time floats) next to the worker, keyed by
`RESEARCH_SPEND_STATE_FILE` (default `~/.research-studio-mcp/spend_window.json`).
Load -> prune (>24h) -> count -> decide -> append -> save, all inside `guard()`.

Robustness: a missing/corrupt state file is treated as an empty window
(log-and-continue, never crash the worker). A write failure after a
successful check is best-effort -- the in-process call is still counted and
enforced for the remainder of this run, it just won't be visible to the next
process if the disk write keeps failing.
"""
import json
import os
import time
from pathlib import Path

_HOUR = 3600
_DAY = 86400

_DEFAULT_STATE_PATH = str(Path.home() / ".research-studio-mcp" / "spend_window.json")

_warnings: list = []  # soft-limit warnings awaiting a drain (nice-to-have, optional consumer)


def _max_per_hour() -> int:
    return int(os.getenv("RESEARCH_ANALYSIS_MAX_PER_HOUR", "20"))


def _max_per_day() -> int:
    return int(os.getenv("RESEARCH_ANALYSIS_MAX_PER_DAY", "80"))


def _state_path(state_path: str | None = None) -> str:
    raw = state_path or os.getenv("RESEARCH_SPEND_STATE_FILE", _DEFAULT_STATE_PATH)
    return os.path.expanduser(raw)


class ResearchSpendCapExceeded(BaseException):
    """A hard Anthropic call-rate cap was hit; the call was blocked (no spend).

    Deliberately a `BaseException`, not `Exception`, so it bypasses
    `worker.run.main`'s broad `except Exception` handler and reaches the CLI
    top level, which should report the cap and exit non-zero WITHOUT
    recording a spurious `status="failed"` analysis row (a capped run is not
    a failed analysis). Carries window/count/limit so the message is exact.
    """

    def __init__(self, window: str, count: int, limit: int):
        self.window = window
        self.count = count
        self.limit = limit
        super().__init__(
            f"Research spend cap hit: {count} calls in the last {window} "
            f"(limit {limit}); analysis blocked."
        )


def _load(path: str) -> list:
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [float(t) for t in data]
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, ValueError, TypeError, OSError) as exc:
        # Corrupt/unreadable state file: never crash the worker over this --
        # treat it as an empty window and keep going.
        print(f"spend_guard: could not read state file {path} ({exc}); treating as empty", file=_stderr())
        return []


def _stderr():
    import sys
    return sys.stderr


def _save(path: str, calls: list) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w") as f:
            json.dump(calls, f)
    except OSError as exc:
        # Best-effort persist: a write failure must not silently disable the
        # cap for the rest of THIS process -- guard() already enforced the
        # check above using the in-memory `calls` list. It just means the
        # next fresh process won't see this call if the disk stays broken.
        print(f"spend_guard: could not persist state file {path} ({exc}); cap still enforced this run", file=_stderr())


def _prune(calls: list, now: float) -> list:
    return [t for t in calls if now - t <= _DAY]


def _count_within(calls: list, now: float, window_secs: int) -> int:
    return sum(1 for t in calls if now - t <= window_secs)


def guard(now: float | None = None, state_path: str | None = None) -> None:
    """Check the rolling caps (loaded from disk), then record this call.

    Call immediately before the Anthropic API hit. Raises
    `ResearchSpendCapExceeded` (blocking the call, so no spend) when a hard
    cap is already met or exceeded -- checked BEFORE appending/persisting the
    new call. On the happy path, appends the call, persists it, and returns.
    """
    now = time.time() if now is None else now
    path = _state_path(state_path)
    max_per_hour = _max_per_hour()
    max_per_day = _max_per_day()

    calls = _prune(_load(path), now)

    in_hour = _count_within(calls, now, _HOUR)
    in_day = _count_within(calls, now, _DAY)

    if in_hour >= max_per_hour:
        raise ResearchSpendCapExceeded("hour", in_hour, max_per_hour)
    if in_day >= max_per_day:
        raise ResearchSpendCapExceeded("day", in_day, max_per_day)

    # Soft warning: an optional heads-up once within ~80% of the hourly cap.
    # Nice-to-have, drainable by a future caller; never blocks.
    soft_threshold = max(1, int(max_per_hour * 0.8))
    if in_hour + 1 >= soft_threshold:
        _warnings.append(
            f"research spend guard: {in_hour + 1}/{max_per_hour} analyses in the last hour "
            f"(hard cap {max_per_hour}/hr, {max_per_day}/day)."
        )

    calls.append(now)
    _save(path, calls)


def drain_warnings() -> list:
    """Return and clear any pending soft-limit warnings."""
    global _warnings
    out, _warnings = _warnings, []
    return out


def snapshot(now: float | None = None, state_path: str | None = None) -> dict:
    """Current window counts + limits, for health/status reporting."""
    now = time.time() if now is None else now
    path = _state_path(state_path)
    calls = _prune(_load(path), now)
    return {
        "calls_last_hour": _count_within(calls, now, _HOUR),
        "calls_last_day": _count_within(calls, now, _DAY),
        "max_per_hour": _max_per_hour(),
        "max_per_day": _max_per_day(),
        "state_path": path,
    }
