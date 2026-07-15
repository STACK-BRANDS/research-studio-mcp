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

Robustness: a missing/corrupt state file on LOAD is treated as an empty
window (log-and-continue, never crash the worker over unreadable data).
But the durable append+save is a PRECONDITION for allowing the call, not a
best-effort afterthought: since each fresh CLI process reloads the window
solely from disk and makes exactly one guarded call, a write failure that
"still enforces the cap this run" enforces nothing at all once `guard()`
returns -- the in-memory `calls` list is discarded and the process goes on to
spend anyway. So a persistence failure (can't acquire the lock, can't write
the state file) FAILS CLOSED: it raises `ResearchSpendCapExceeded`, blocking
the call, rather than silently disabling the cap.

Cross-process synchronization: the whole load -> prune -> count -> decide ->
append -> save sequence runs inside an `fcntl.flock` exclusive lock on a
sibling `<state_path>.lock` file, so two concurrent CLI processes can't both
load the same pre-write count, both pass, and both overwrite each other's
save. The save itself is atomic (write a temp file + `os.replace`) so a crash
mid-write can't corrupt the window.
"""
import errno
import fcntl
import json
import os
import time
from pathlib import Path

_HOUR = 3600
_DAY = 86400
_LOCK_TIMEOUT_SECS = 5.0
_LOCK_POLL_SECS = 0.05

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
    """The call was blocked -- either a hard rate cap was hit, or the guard
    could not durably enforce the cap (lock/persist failure) and fails closed.

    Deliberately a `BaseException`, not `Exception`, so it bypasses
    `worker.run.main`'s broad `except Exception` handler and reaches the CLI
    top level, which should report the block and exit non-zero WITHOUT
    recording a spurious `status="failed"` analysis row (a blocked run is not
    a failed analysis).

    `window` is one of `"hour"` / `"day"` for a real cap hit, or `"lock"` /
    `"persist"` for a fail-closed guard-infrastructure failure -- callers that
    branch on `window` can tell the two apart. `reason`, when given,
    overrides the default cap-hit message with an explicit explanation of
    *why* the call was blocked (so "state persistence failed" never looks
    like an ordinary cap hit in logs).
    """

    def __init__(self, window: str, count: int, limit: int, reason: str | None = None):
        self.window = window
        self.count = count
        self.limit = limit
        message = reason if reason is not None else (
            f"Research spend cap hit: {count} calls in the last {window} "
            f"(limit {limit}); analysis blocked."
        )
        super().__init__(message)


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
    """Atomically persist the call list: write a sibling temp file, fsync it,
    then `os.replace` it over the real path. `os.replace` is atomic on both
    POSIX and Windows, so a crash mid-write leaves either the old, complete
    file or nothing -- never a half-written/corrupt one.

    Raises `OSError` on any failure (can't make the parent dir, can't write,
    can't replace). This is NOT best-effort: `guard()` treats a raised
    `OSError` here as a precondition failure and fails the call closed --
    see the module docstring for why persistence must be a precondition,
    not an afterthought, for a one-call-per-process CLI.
    """
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w") as f:
            json.dump(calls, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass  # best-effort cleanup only; the original OSError still propagates
        raise


def _acquire_lock(lock_path: str):
    """Acquire an exclusive `fcntl.flock` on a sibling lock file, guarding the
    entire load -> prune -> count -> decide -> append -> save critical
    section against concurrent `python -m worker.run` processes.

    Polls a non-blocking `flock` for up to `_LOCK_TIMEOUT_SECS` rather than
    blocking forever, so a wedged lock can't hang the worker indefinitely.
    Returns the open file object (the lock is released via `_release_lock`).
    Raises `OSError` (including its `TimeoutError` subclass) if the lock
    can't be created or acquired in time -- callers must fail closed on this.
    """
    parent = os.path.dirname(lock_path) or "."
    os.makedirs(parent, exist_ok=True)
    fd = open(lock_path, "a+")
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                fd.close()
                raise
            if time.monotonic() >= deadline:
                fd.close()
                raise TimeoutError(
                    f"could not acquire spend-guard lock {lock_path} within {_LOCK_TIMEOUT_SECS}s"
                ) from exc
            time.sleep(_LOCK_POLL_SECS)


def _release_lock(fd) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        fd.close()


def _prune(calls: list, now: float) -> list:
    return [t for t in calls if now - t <= _DAY]


def _count_within(calls: list, now: float, window_secs: int) -> int:
    return sum(1 for t in calls if now - t <= window_secs)


def guard(now: float | None = None, state_path: str | None = None) -> None:
    """Check the rolling caps (loaded from disk), then record this call --
    the whole thing under an inter-process lock, so two concurrent CLI
    processes can't both load the same pre-write count and both pass.

    Call immediately before the Anthropic API hit. Raises
    `ResearchSpendCapExceeded` (blocking the call, so no spend) when:
      - a hard cap is already met or exceeded (checked BEFORE
        appending/persisting the new call), or
      - the lock can't be acquired, or
      - the durable save fails.
    The latter two are fail-closed: this guard's only job is to reliably cap
    spend across separate processes, so if it can't reliably record this
    call, the call must not be allowed to happen.

    On the happy path: appends the call, persists it atomically, and
    returns.
    """
    now = time.time() if now is None else now
    path = _state_path(state_path)
    lock_path = f"{path}.lock"
    max_per_hour = _max_per_hour()
    max_per_day = _max_per_day()

    try:
        lock_fd = _acquire_lock(lock_path)
    except OSError as exc:
        raise ResearchSpendCapExceeded(
            "lock", 0, 0,
            reason=(
                f"Research spend guard: could not acquire spend-guard lock {lock_path} "
                f"({exc}); analysis blocked (fail closed) -- proceeding unguarded could "
                "let concurrent runs both pass the cap check and overwrite each other's state."
            ),
        ) from exc

    try:
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
        try:
            _save(path, calls)
        except OSError as exc:
            raise ResearchSpendCapExceeded(
                "persist", len(calls), max_per_hour,
                reason=(
                    f"Research spend guard: state persistence failed for {path} ({exc}); "
                    "analysis blocked (fail closed) -- an unwritable ledger must not "
                    "silently disable the cap for the next process."
                ),
            ) from exc
    finally:
        _release_lock(lock_fd)


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
