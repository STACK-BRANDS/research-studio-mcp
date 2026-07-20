"""Fleet usage reporter -- bot-side client for POST /api/usage/ingest.

Canonical copy: bot-infra/shared/usage_reporter.py. Distribution is
copy-per-repo (no private-package infra exists for the STACK-BRANDS GitHub
user account); every copy MUST stay byte-identical to this file --
USAGE_REPORTER_VERSION below is the drift marker a hash-compare check can use
(see bot-infra/shared/README.md).

Interface (fleet-usage-standard design spec, 2026-07-15, §4.1):

    from usage_reporter import UsageReporter

    usage = UsageReporter(system="research", repo="research-studio-mcp")
    # env: USAGE_INGEST_URL, USAGE_INGEST_TOKEN; missing -> permanent no-op,
    # one stderr line, no network ever attempted.

    usage.spend(action="rs-worker/analyze-competitor", model=resp.model,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                store_id=store_id, meta={"angle": angle})

    usage.cost(action="image-gen/hero", cost=0.04, quantity=1, unit="images")

    usage.activity(action="sheets-read", quantity=3, unit="calls")

    usage.flush()          # explicit, at end of run
    # atexit + on-buffer-full (25 events) flushes happen automatically.

Behavioral contract (§4.2, "never break the bot"):
  - Never raises. Every public method and the flush path are wrapped in a
    broad `except Exception` -> one stderr log line. No exception from this
    module can escape into bot logic.
  - Bounded time, NOT zero latency. "No threads" (below) means a flush is
    synchronous: the call that triggers it -- every 25th buffered event, and
    the end-of-run flush() -- blocks up to (POST timeout 3s x 2 attempts) = 6s
    per batch while it POSTs. That is deliberate and bounded (a hung endpoint
    can delay a run by seconds, never hang it). It is fine for the fleet's
    workers, which are batch Cloud Run Jobs, not latency-critical request
    handlers. Do NOT call the reporter on a path that must answer within a tight
    deadline (e.g. a Discord interaction ack); if you ever need that, report
    from the batch worker, not the trigger. Retry is one immediate resend (same
    dedupe_keys -> idempotent server-side), no backoff loop.
  - Bounded memory: buffer cap 500 events; overflow drops the OLDEST events
    and counts the drop into a final `reporter/dropped` activity event.
  - No threads spawned. Flushes are synchronous (buffer-full / explicit /
    atexit) -- a daemon thread is exactly how a reporter would end up
    breaking a bot. BUT the buffer IS thread-safe: a `threading.Lock`
    guards every buffer mutation/drain, so the reporter may be CALLED
    concurrently from multiple threads (e.g. a discord.py bot analyzing
    several cases via asyncio.to_thread while another coroutine flushes in
    an executor) without losing or double-sending events. The lock is never
    held across the network POST -- draining takes a snapshot under the lock,
    then POSTs outside it.
  - dedupe_key is generated per event at creation time:
    f"{repo}:{run_id}:{uuid4().hex[:12]}".
  - The server prices tokens (fleet-usage-standard spec §2.1) -- this module
    carries NO pricing table and never computes cost from tokens itself.
"""
import atexit
import os
import sys
import threading
import time
import uuid

import requests

USAGE_REPORTER_VERSION = "4"  # v4: auto_flush=False for latency-sensitive callers

_BUFFER_FLUSH_SIZE = 25
_BUFFER_MAX_SIZE = 500
_POST_TIMEOUT_SECS = 3
_MAX_ATTEMPTS = 2  # 1 initial attempt + at most 1 retry
_MAX_EVENTS_PER_BATCH = 50


def _log(msg: str) -> None:
    print(f"[usage_reporter] {msg}", file=sys.stderr)


class UsageReporter:
    """Buffers usage events in memory and flushes them to the fleet usage
    ingest endpoint. See the module docstring for the full contract.
    """

    def __init__(self, system: str, repo: str, run_id: str = None, auto_flush: bool = True):
        self.system = system
        self.repo = repo
        self.run_id = run_id or f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}"
        # auto_flush=True (default, batch workers): spend()/activity() flush
        # synchronously once the buffer hits _BUFFER_FLUSH_SIZE. auto_flush=False
        # (latency-sensitive / user-facing callers, e.g. a Discord assistant
        # reply path): record() ONLY buffers -- it NEVER POSTs on the caller's
        # path -- so the caller is guaranteed non-blocking; the owner must drive
        # flush() itself (a periodic background task). The 500-event drop-oldest
        # cap still applies, so memory stays bounded even if a flush never runs.
        self._auto_flush = auto_flush
        self._buffer = []
        self._dropped = 0
        self._lock = threading.Lock()  # guards _buffer / _dropped mutation + drain
        self._url = os.environ.get("USAGE_INGEST_URL")
        self._token = os.environ.get("USAGE_INGEST_TOKEN")
        self._noop = not self._url or not self._token
        if self._noop:
            _log(
                "USAGE_INGEST_URL/USAGE_INGEST_TOKEN not set -- reporter is a "
                "permanent no-op for this process."
            )
        else:
            atexit.register(self._atexit_flush)

    # -- public event methods -------------------------------------------

    def spend(self, action, model=None, input_tokens=None, output_tokens=None,
              cache_creation_input_tokens=None, cache_read_input_tokens=None,
              cost=None, quantity=None, unit=None, store_id=None, meta=None):
        """Token-priced (tier-2) or provider-billed (tier-1, if `cost` is
        also given) LLM spend. Never invent a price client-side -- the
        server derives cost from (model, input/output tokens, and the Anthropic
        prompt-cache token categories cache_creation_input_tokens /
        cache_read_input_tokens, which are billed separately). Pass the cache
        fields straight from `resp.usage` when the call used prompt caching --
        omitting them materially underprices a cached call."""
        self._safe(self._record, action, "spend", model=model,
                   input_tokens=input_tokens, output_tokens=output_tokens,
                   cache_creation_input_tokens=cache_creation_input_tokens,
                   cache_read_input_tokens=cache_read_input_tokens,
                   cost=cost, quantity=quantity, unit=unit, store_id=store_id,
                   meta=meta)

    def cost(self, action, cost, quantity=None, unit=None, store_id=None, meta=None):
        """Tier-1: the provider reports the billed amount directly."""
        self._safe(self._record, action, "spend", cost=cost, quantity=quantity,
                   unit=unit, store_id=store_id, meta=meta)

    def activity(self, action, quantity=None, unit=None, store_id=None, meta=None):
        """Non-priced calls (Sheets, Shopify, Discord, flat-rate API hits, ...)."""
        self._safe(self._record, action, "activity", quantity=quantity,
                   unit=unit, store_id=store_id, meta=meta)

    def flush(self) -> None:
        """Explicit flush. Never raises."""
        self._safe(self._flush)

    # -- internals --------------------------------------------------------

    def _record(self, action, kind, model=None, input_tokens=None,
                output_tokens=None, cache_creation_input_tokens=None,
                cache_read_input_tokens=None, cost=None, quantity=None,
                unit=None, store_id=None, meta=None):
        if self._noop:
            return
        event = {
            "system": self.system,
            "action": action,
            "dedupe_key": f"{self.repo}:{self.run_id}:{uuid.uuid4().hex[:12]}",
            "actor": f"bot:{self.repo}",
            "kind": kind,
        }
        if cost is not None:
            event["cost"] = cost
        if model is not None:
            event["model"] = model
        if input_tokens is not None:
            event["input_tokens"] = input_tokens
        if output_tokens is not None:
            event["output_tokens"] = output_tokens
        if cache_creation_input_tokens is not None:
            event["cache_creation_input_tokens"] = cache_creation_input_tokens
        if cache_read_input_tokens is not None:
            event["cache_read_input_tokens"] = cache_read_input_tokens
        if quantity is not None:
            event["quantity"] = quantity
        if unit is not None:
            event["unit"] = unit
        if store_id is not None:
            event["store_id"] = store_id
        if meta is not None:
            event["metadata"] = meta

        # Mutate the shared buffer only under the lock (this may run in a
        # worker thread while another thread flushes) -- but decide-to-flush
        # here and actually flush OUTSIDE the lock, so the network POST never
        # serializes concurrent recorders.
        with self._lock:
            self._buffer.append(event)
            if len(self._buffer) > _BUFFER_MAX_SIZE:
                # Bounded memory: drop the OLDEST events, keep the newest. The
                # drop count rides along as one reporter/dropped activity event
                # on the next flush, so the loss itself is visible fleet-side.
                overflow = len(self._buffer) - _BUFFER_MAX_SIZE
                del self._buffer[:overflow]
                self._dropped += overflow
            should_flush = self._auto_flush and len(self._buffer) >= _BUFFER_FLUSH_SIZE
        if should_flush:
            self._flush()

    def _flush(self) -> None:
        if self._noop:
            return
        # Drain the whole buffer atomically UNDER the lock into a local list,
        # then release the lock and POST. Because each event is drained exactly
        # once, concurrent flushes (multiple threads) can never double-send or
        # lose events, and recorders appending during the POST are unaffected.
        with self._lock:
            if self._dropped:
                self._buffer.append({
                    "system": self.system,
                    "action": "reporter/dropped",
                    "dedupe_key": f"{self.repo}:{self.run_id}:{uuid.uuid4().hex[:12]}",
                    "actor": f"bot:{self.repo}",
                    "kind": "activity",
                    "quantity": self._dropped,
                    "unit": "events",
                })
                self._dropped = 0
            if not self._buffer:
                return
            to_send = self._buffer
            self._buffer = []
        # Send in batches of at most 50 events per the wire contract, OUTSIDE
        # the lock. A batch is dropped (logged) if the POST fails -- no retry
        # queue (bounded memory, no backoff loop).
        for i in range(0, len(to_send), _MAX_EVENTS_PER_BATCH):
            self._post(to_send[i:i + _MAX_EVENTS_PER_BATCH])

    def _post(self, batch) -> None:
        payload = {"events": batch}
        headers = {"Authorization": f"Bearer {self._token}"}
        last_exc = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = requests.post(self._url, json=payload, headers=headers,
                                     timeout=_POST_TIMEOUT_SECS)
            except Exception as exc:  # noqa: BLE001 -- never raise past this module
                last_exc = exc
                # No backoff sleep: one immediate retry (same payload, same
                # dedupe_keys -> idempotent server-side), then give up.
                continue
            # A non-2xx is NOT retried (a 400/401/429 won't fix itself on an
            # immediate resend) but IS logged -- a silently-swallowed 401 is
            # exactly what makes a mis-tokened pilot look like "no data" with
            # no clue why. Read defensively: a missing/odd status must never
            # itself raise out of the never-break flush path.
            status = getattr(resp, "status_code", None)
            if isinstance(status, int) and status >= 300:
                try:
                    body = resp.text[:200]
                except Exception:  # noqa: BLE001
                    body = "<unreadable body>"
                _log(f"ingest returned HTTP {status} for {len(batch)} event(s): {body}")
            return
        _log(f"flush failed after {_MAX_ATTEMPTS} attempt(s): {last_exc}")

    def _atexit_flush(self) -> None:
        self._safe(self._flush)

    def _safe(self, fn, *args, **kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- the module-wide never-raise contract
            _log(f"unexpected error in {fn.__name__}: {exc}")
