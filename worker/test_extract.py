"""Tests for the `collect` job (P3 EXTRACTION of the deep-research plan):
worker/extract.py's `run_collect` + its pure helpers, and the four new worker/store.py
RPC/storage wrappers it depends on (get_site_capture, download_capture_object,
create_voc_quote, create_finding).

No network, no real Supabase, no real Anthropic call: `store._client()` is monkeypatched
to fake query-builder/RPC/storage clients (mirroring test_budget.py's and
test_store_registry.py's own fakes), and `run_collect`'s collaborators (`extract.store`,
`extract.jobs.assert_lease`, `extract.budget.reserve`/`settle`, `extract.Anthropic`,
`extract.usage_reporter`) are monkeypatched directly on the module objects that use them
-- the same pattern test_connectors.py uses for web_fetch.capture_pages.
"""
import json

import pytest

from worker import extract, store
from worker.budget import ReserveResult
from worker.canonical import canonical_text


# ===========================================================================
# worker/store.py -- the four new P3 wrappers, tested directly against fake
# Supabase clients (query-builder for get_site_capture, RPC for the two mint
# functions, storage for download_capture_object).
# ===========================================================================

class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filters = {}

    def select(self, *_a, **_k):
        return self

    def eq(self, field, value):
        self.filters[field] = value
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        matching = [
            r for r in self._rows
            if all(r.get(k) == v for k, v in self.filters.items())
        ]
        return _FakeResult(matching)


class _FakeTableClient:
    def __init__(self, rows):
        self._rows = rows
        self.queries = []

    def table(self, _name):
        q = _FakeQuery(self._rows)
        self.queries.append(q)
        return q


def test_get_site_capture_returns_row(monkeypatch):
    rows = [{
        "id": "cap-1", "competitor_id": "comp-1", "url": "https://x.com/p",
        "source_url": "https://x.com/p", "captured_html_path": "captures/rawsha.html",
        "content_sha256": "canonsha", "connector": "web.fetch",
    }]
    monkeypatch.setattr(store, "_client", lambda: _FakeTableClient(rows))
    row = store.get_site_capture("cap-1")
    assert row["content_sha256"] == "canonsha"
    assert row["captured_html_path"] == "captures/rawsha.html"


def test_get_site_capture_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(store, "_client", lambda: _FakeTableClient([]))
    assert store.get_site_capture("no-such-id") is None


class _FakeBucket:
    def __init__(self, objects=None, raise_exc=None):
        self._objects = objects or {}
        self._raise_exc = raise_exc
        self.downloaded = []

    def download(self, path):
        self.downloaded.append(path)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._objects[path]


class _FakeStorage:
    def __init__(self, bucket):
        self._bucket = bucket

    def from_(self, _name):
        return self._bucket


class _FakeStorageClient:
    def __init__(self, bucket):
        self.storage = _FakeStorage(bucket)


def test_download_capture_object_returns_bytes(monkeypatch):
    bucket = _FakeBucket(objects={"captures/abc.txt": b"hello canonical text"})
    monkeypatch.setattr(store, "_client", lambda: _FakeStorageClient(bucket))
    data = store.download_capture_object("captures/abc.txt")
    assert data == b"hello canonical text"
    assert bucket.downloaded == ["captures/abc.txt"]


def test_download_capture_object_propagates_failure(monkeypatch):
    bucket = _FakeBucket(raise_exc=RuntimeError("object not found"))
    monkeypatch.setattr(store, "_client", lambda: _FakeStorageClient(bucket))
    with pytest.raises(RuntimeError, match="object not found"):
        store.download_capture_object("captures/missing.txt")


class _FakeRpc:
    def __init__(self, name, params, response_data):
        self.name = name
        self.params = params
        self._response_data = response_data

    def execute(self):
        return _FakeResult(self._response_data)


class _FakeRpcClient:
    def __init__(self, rpc_response=None, raise_exc=None):
        self.rpc_response = rpc_response
        self.raise_exc = raise_exc
        self.rpcs = []

    def rpc(self, name, params):
        if self.raise_exc is not None:
            raise self.raise_exc
        r = _FakeRpc(name, params, self.rpc_response)
        self.rpcs.append(r)
        return r


def test_create_voc_quote_sends_expected_params_with_all_fields(monkeypatch):
    client = _FakeRpcClient(rpc_response="quote-id-1")
    monkeypatch.setattr(store, "_client", lambda: client)

    quote_id = store.create_voc_quote(
        quote="so comfy", source_url="https://x.com/p", raw_content="review text so comfy",
        connector="web.fetch", competitor_id="comp-1", store_id="mv", quote_type="desire",
        theme="comfort", product_area="bras", confidence="high",
        source_start=10, source_end=18,
    )
    assert quote_id == "quote-id-1"
    assert client.rpcs[0].name == "rs_create_voc_quote"
    assert client.rpcs[0].params == {
        "p_quote": "so comfy", "p_source_url": "https://x.com/p",
        "p_raw_content": "review text so comfy", "p_connector": "web.fetch",
        "p_competitor_id": "comp-1", "p_store_id": "mv", "p_type": "desire",
        "p_theme": "comfort", "p_product_area": "bras", "p_confidence": "high",
        "p_source_start": 10, "p_source_end": 18,
    }


def test_create_voc_quote_omits_optional_params_when_none(monkeypatch):
    client = _FakeRpcClient(rpc_response="quote-id-2")
    monkeypatch.setattr(store, "_client", lambda: client)
    store.create_voc_quote(
        quote="ok", source_url="https://x.com/p", raw_content="ok text",
        connector="web.fetch",
    )
    assert client.rpcs[0].params == {
        "p_quote": "ok", "p_source_url": "https://x.com/p",
        "p_raw_content": "ok text", "p_connector": "web.fetch",
    }


def test_create_voc_quote_propagates_rpc_exception(monkeypatch):
    """NOT best-effort: the A-1 verbatim check (or any other RPC failure)
    must propagate -- the caller (run_collect's admission control) is the
    one place that catches and counts this as a drop."""
    client = _FakeRpcClient(raise_exc=RuntimeError("not verbatim-present"))
    monkeypatch.setattr(store, "_client", lambda: client)
    with pytest.raises(RuntimeError, match="not verbatim-present"):
        store.create_voc_quote(quote="fabricated", source_url="u", raw_content="c", connector="web.fetch")


def test_create_finding_sends_expected_params_with_all_fields(monkeypatch):
    client = _FakeRpcClient(rpc_response="finding-id-1")
    monkeypatch.setattr(store, "_client", lambda: client)

    finding_id = store.create_finding(
        finding_kind="site_fact", schema_version=1,
        payload={"fact_type": "pricing", "statement": "Buy 2 get 1 free"},
        source_url="https://x.com/p", raw_content="page text", connector="web.fetch",
        project_id="proj-1", store_id="mv", confidence="medium",
    )
    assert finding_id == "finding-id-1"
    assert client.rpcs[0].name == "rs_create_finding"
    assert client.rpcs[0].params == {
        "p_finding_kind": "site_fact", "p_schema_version": 1,
        "p_payload": {"fact_type": "pricing", "statement": "Buy 2 get 1 free"},
        "p_source_url": "https://x.com/p", "p_raw_content": "page text",
        "p_connector": "web.fetch", "p_project_id": "proj-1",
        "p_store_id": "mv", "p_confidence": "medium",
    }


def test_create_finding_omits_optional_params_when_none(monkeypatch):
    client = _FakeRpcClient(rpc_response="finding-id-2")
    monkeypatch.setattr(store, "_client", lambda: client)
    store.create_finding(
        finding_kind="site_fact", schema_version=1, payload={"fact_type": "policy", "statement": "s"},
        source_url="u", raw_content="c", connector="web.fetch",
    )
    assert client.rpcs[0].params == {
        "p_finding_kind": "site_fact", "p_schema_version": 1,
        "p_payload": {"fact_type": "policy", "statement": "s"},
        "p_source_url": "u", "p_raw_content": "c", "p_connector": "web.fetch",
    }


def test_create_finding_propagates_rpc_exception(monkeypatch):
    client = _FakeRpcClient(raise_exc=RuntimeError("not a registered finding kind/version"))
    monkeypatch.setattr(store, "_client", lambda: client)
    with pytest.raises(RuntimeError, match="not a registered finding kind/version"):
        store.create_finding(
            finding_kind="bogus_kind", schema_version=1, payload={}, source_url="u",
            raw_content="c", connector="web.fetch",
        )


# ===========================================================================
# worker/extract.py -- pure helpers.
# ===========================================================================

def test_normalize_text_collapses_whitespace_and_trims():
    assert extract.normalize_text("  lots\n\n  of   whitespace  ") == "lots of whitespace"


def test_normalize_text_maps_curly_quotes_to_straight():
    assert extract.normalize_text("it’s “great”") == "it's \"great\""


def test_normalize_text_strips_zero_width_characters():
    assert extract.normalize_text("hello​world") == "helloworld"


def test_normalize_text_does_not_case_fold():
    """'verbatim' must stay verbatim -- rs_normalize_text (migration 142) explicitly
    does NOT case-fold, and this Python mirror must not either."""
    assert extract.normalize_text("Hello World") == "Hello World"
    assert extract.normalize_text("Hello World") != extract.normalize_text("hello world")


def test_normalize_text_empty_input():
    assert extract.normalize_text("") == ""
    assert extract.normalize_text(None) == ""


def test_find_exact_span_locates_substring():
    span = extract._find_exact_span("the quick brown fox", "quick brown")
    assert span == (4, 15)
    assert "the quick brown fox"[span[0]:span[1]] == "quick brown"


def test_find_exact_span_returns_none_when_absent():
    assert extract._find_exact_span("the quick brown fox", "slow turtle") is None


def test_actual_cents_rounds_up_and_uses_documented_rates():
    # 1,000,000 input tokens @ $3/MTok + 1,000,000 output tokens @ $15/MTok = $18 = 1800 cents.
    assert extract._actual_cents(1_000_000, 1_000_000) == 1800
    # A tiny call must still round UP to at least 1 cent, never down to 0.
    assert extract._actual_cents(10, 10) >= 1


def test_build_extraction_schema_shape():
    schema = extract.build_extraction_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"quotes", "findings"}

    quote_schema = schema["properties"]["quotes"]["items"]
    assert quote_schema["additionalProperties"] is False
    assert quote_schema["properties"]["type"]["enum"] == extract.QUOTE_TYPES
    assert set(quote_schema["required"]) == {"quote", "type", "theme", "product_area", "confidence"}

    finding_schema = schema["properties"]["findings"]["items"]
    assert finding_schema["additionalProperties"] is False
    assert finding_schema["properties"]["fact_type"]["enum"] == extract.FACT_TYPES
    assert set(finding_schema["required"]) == {
        "fact_type", "statement", "detail", "evidence", "confidence",
    }


# ===========================================================================
# run_collect -- full integration, every collaborator faked.
# ===========================================================================

class _FakeStore:
    """Records every mint call; raises KeyError on an unset lookup so a test
    is forced to wire exactly what it needs. `raise_on_quote` simulates the
    RPC itself rejecting a specific quote text (the A-1 backstop firing on
    something this module's own pre-check let through) -- a CONTENT rejection.
    `infra_raise_on_quote`/`infra_raise_on_finding` simulate an INFRA failure
    (DB/network outage, a client timeout after a server-side commit, ...) --
    something that must PROPAGATE (FIX P2), never be miscounted as a drop."""

    def __init__(self, capture=None, objects=None, raise_on_quote=None,
                 infra_raise_on_quote=None, infra_raise_on_finding=None,
                 collect_done=False):
        self.capture = capture
        self.objects = objects or {}
        self.raise_on_quote = raise_on_quote or set()
        self.infra_raise_on_quote = infra_raise_on_quote or set()
        self.infra_raise_on_finding = infra_raise_on_finding or set()
        self.collect_done = collect_done
        self.voc_quotes = []
        self.findings = []

    def get_site_capture(self, capture_id):
        return self.capture

    def collect_done_exists(self, capture_id, extractor_version):
        return self.collect_done

    def download_capture_object(self, path):
        return self.objects[path]

    def create_voc_quote(self, quote, source_url, raw_content, connector,
                          competitor_id=None, store_id=None, quote_type=None,
                          theme=None, product_area=None, confidence=None,
                          source_start=None, source_end=None):
        if quote in self.raise_on_quote:
            raise RuntimeError(f"rs_create_voc_quote: p_quote is not verbatim-present ({quote!r})")
        if quote in self.infra_raise_on_quote:
            raise RuntimeError("connection reset by peer")
        row = {
            "quote": quote, "source_url": source_url, "raw_content": raw_content,
            "connector": connector, "competitor_id": competitor_id, "store_id": store_id,
            "type": quote_type, "theme": theme, "product_area": product_area,
            "confidence": confidence, "source_start": source_start, "source_end": source_end,
        }
        self.voc_quotes.append(row)
        return f"quote-{len(self.voc_quotes)}"

    def create_finding(self, finding_kind, schema_version, payload, source_url, raw_content,
                        connector, project_id=None, store_id=None, confidence=None):
        if payload.get("statement") in self.infra_raise_on_finding:
            raise RuntimeError("connection reset by peer")
        row = {
            "finding_kind": finding_kind, "schema_version": schema_version, "payload": payload,
            "source_url": source_url, "raw_content": raw_content, "connector": connector,
            "project_id": project_id, "store_id": store_id, "confidence": confidence,
        }
        self.findings.append(row)
        return f"finding-{len(self.findings)}"


class _FakeUsage:
    def __init__(self, input_tokens=100, output_tokens=50):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, payload, input_tokens=100, output_tokens=50):
        self.content = [_FakeBlock(json.dumps(payload))]
        self.usage = _FakeUsage(input_tokens, output_tokens)


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get_final_message(self):
        return self._resp


class _FakeAnthropicClient:
    def __init__(self, resp, calls):
        self._resp = resp
        self._calls = calls

    @property
    def messages(self):
        return self

    def stream(self, **kwargs):
        self._calls.append(kwargs)
        return _FakeStreamCtx(self._resp)


RAW_HTML = (
    "<html><body>"
    "<div class='hero'>Buy 2 get 1 free, today only!</div>"
    "<div class='jdgm-rev-widg'><div class='jdgm-rev__body'>"
    "incredibly comfortable and true to size"
    "</div></div>"
    "</body></html>"
)
CANONICAL_TEXT = canonical_text(RAW_HTML)


def _capture(**overrides):
    row = {
        "id": "cap-1", "competitor_id": "comp-1", "url": "https://competitor.com/p",
        "source_url": "https://competitor.com/p", "captured_html_path": "captures/rawsha.html",
        "content_sha256": "canonsha", "connector": "web.fetch",
    }
    row.update(overrides)
    return row


def _wire(monkeypatch, fake_store, resp, lease_values=True, reserve_result=None):
    # settings.anthropic_api_key reads os.environ["ANTHROPIC_API_KEY"] directly (no
    # default) -- set a dummy value so building the (faked) Anthropic client never
    # raises KeyError in a test environment with no real .env.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(extract, "store", fake_store)

    if isinstance(lease_values, list):
        it = iter(lease_values)
        monkeypatch.setattr(extract.jobs, "assert_lease", lambda job_id, claimant: next(it))
    else:
        monkeypatch.setattr(extract.jobs, "assert_lease", lambda job_id, claimant: lease_values)

    # NOTE: `reserve_result or DEFAULT` would be wrong here -- ReserveResult.__bool__
    # returns `self.ok`, so an explicitly-passed ok=False (skip) result is itself
    # falsy and `or` would silently discard it in favor of the ok=True default. Use an
    # explicit `is None` check instead.
    if reserve_result is None:
        reserve_result = ReserveResult(ok=True, reserved_est_cents=25, project_scoped=True)
    reserve_calls = []

    def _fake_reserve(job, ref, claimant, connector=None):
        reserve_calls.append((job, ref, claimant, connector))
        return reserve_result

    settle_calls = []

    def _fake_settle(job, ref, actual_cents, claimant, reserved_est, report_usage=True):
        settle_calls.append({
            "job": job, "ref": ref, "actual_cents": actual_cents,
            "claimant": claimant, "reserved_est": reserved_est, "report_usage": report_usage,
        })

    monkeypatch.setattr(extract.budget, "reserve", _fake_reserve)
    monkeypatch.setattr(extract.budget, "settle", _fake_settle)

    anthropic_calls = []
    monkeypatch.setattr(extract, "Anthropic", lambda api_key: _FakeAnthropicClient(resp, anthropic_calls))

    usage_calls = []
    monkeypatch.setattr(extract.usage_reporter, "spend", lambda **kw: usage_calls.append(kw))

    return {
        "reserve_calls": reserve_calls, "settle_calls": settle_calls,
        "anthropic_calls": anthropic_calls, "usage_calls": usage_calls,
    }


def _job(**overrides):
    job = {
        "id": "job-1", "job_kind": "collect", "project_id": "proj-1",
        "params": {"capture_id": "cap-1", "extractor_version": "text@v2",
                    "project_id": "proj-1", "store_id": "mv"},
    }
    job.update(overrides)
    return job


def test_run_collect_capture_not_found_raises(monkeypatch):
    fake_store = _FakeStore(capture=None)
    monkeypatch.setattr(extract, "store", fake_store)
    with pytest.raises(ValueError, match="cap-1"):
        extract.run_collect(_job(), "claimant-1")


def test_run_collect_lease_lost_before_reserve_raises_and_never_reserves(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    resp = _FakeResponse({"quotes": [], "findings": []})
    hooks = _wire(monkeypatch, fake_store, resp, lease_values=False)

    with pytest.raises(RuntimeError, match="lease"):
        extract.run_collect(_job(), "claimant-1")

    assert hooks["reserve_calls"] == []
    assert hooks["anthropic_calls"] == []


def test_run_collect_lease_lost_before_paid_call_raises_and_releases_reservation(monkeypatch):
    """PRE-CALL lease loss (FIX 2): no API call was ever made, so nothing was actually
    spent -- but the reservation itself must not be left orphaned. It is released
    (settle actual=0) before the failure propagates."""
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    resp = _FakeResponse({"quotes": [], "findings": []})
    # First assert_lease call (pre-reserve) True, second (pre-paid-call) False.
    hooks = _wire(monkeypatch, fake_store, resp, lease_values=[True, False])

    with pytest.raises(RuntimeError, match="lease"):
        extract.run_collect(_job(), "claimant-1")

    assert len(hooks["reserve_calls"]) == 1
    assert hooks["anthropic_calls"] == []
    # Exactly one settle, releasing the reserve (actual_cents == 0) -- never orphaned.
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 0
    assert hooks["settle_calls"][0]["reserved_est"] == 25


def test_run_collect_settles_worst_case_when_the_paid_call_fails(monkeypatch):
    """FIX 3: if the Anthropic call raises, the reservation must be settled at its WORST
    CASE (the reserved estimate), not actual=0 -- a partial/mid-stream failure may have
    billed real tokens, and under-counting on a failed call could let the project's
    ceiling be silently exceeded; over-counting is ceiling-safe. Nothing is minted."""
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    hooks = _wire(monkeypatch, fake_store, _FakeResponse({"quotes": [], "findings": []}))

    class _Raising:
        @property
        def messages(self):
            return self

        def stream(self, **kwargs):
            raise RuntimeError("stream boom mid-call")

    monkeypatch.setattr(extract, "Anthropic", lambda api_key: _Raising())

    with pytest.raises(RuntimeError, match="boom"):
        extract.run_collect(_job(), "claimant-1")

    # Exactly one settle, at the WORST CASE (reserved_est), never actual=0.
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == hooks["settle_calls"][0]["reserved_est"]
    assert hooks["settle_calls"][0]["actual_cents"] == 25
    assert fake_store.voc_quotes == []
    assert fake_store.findings == []


def test_run_collect_lease_lost_after_call_before_mint_skips_all_mints(monkeypatch):
    """Lease lost during the paid call: the spend is settled (honest), but a revoked worker
    must NOT mint (it would duplicate the new claimant's) -- it raises before any mint."""
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    resp = _FakeResponse({
        "quotes": [{"quote": "incredibly comfortable and true to size", "type": "desire",
                     "theme": "comfort", "product_area": "bras", "confidence": "high"}],
        "findings": [{"fact_type": "pricing", "statement": "Buy 2 get 1 free", "detail": "",
                       "evidence": "Buy 2 get 1 free", "confidence": "medium"}],
    })
    # pre-reserve True, pre-paid-call True, pre-mint (first quote) False.
    hooks = _wire(monkeypatch, fake_store, resp, lease_values=[True, True, False])

    with pytest.raises(RuntimeError, match="before minting"):
        extract.run_collect(_job(), "claimant-1")

    # The call ran and was settled (real spend recorded), but NOTHING was minted.
    assert len(hooks["settle_calls"]) == 1
    assert fake_store.voc_quotes == []
    assert fake_store.findings == []


def test_run_collect_reserve_skip_short_circuits_without_calling_anthropic(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    resp = _FakeResponse({"quotes": [], "findings": []})
    skip_result = ReserveResult(ok=False, reserved_est_cents=25, project_scoped=True)
    hooks = _wire(monkeypatch, fake_store, resp, reserve_result=skip_result)

    result = extract.run_collect(_job(), "claimant-1")

    assert result == {
        "quotes_minted": 0, "quotes_rejected": 0,
        "findings_minted": 0, "findings_rejected": 0,
        "review_chars": len(extract.extract_review_text(RAW_HTML)),
    }
    assert hooks["anthropic_calls"] == []
    assert fake_store.voc_quotes == []
    assert fake_store.findings == []


def test_run_collect_happy_path_mints_valid_quotes_and_findings(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    payload = {
        "quotes": [
            {"quote": "incredibly comfortable and true to size", "type": "desire",
             "theme": "comfort", "product_area": "bras", "confidence": "high"},
            {"quote": "this text does not appear anywhere in the review", "type": "irritation",
             "theme": "x", "product_area": "y", "confidence": "low"},
            {"quote": "incredibly comfortable and true to size", "type": "not-a-real-type",
             "theme": "x", "product_area": "y", "confidence": "low"},
        ],
        "findings": [
            {"fact_type": "pricing", "statement": "Buy 2 get 1 free", "detail": "",
             "evidence": "today only!", "confidence": "medium"},
            {"fact_type": "not-a-real-fact-type", "statement": "whatever", "detail": "",
             "evidence": "whatever", "confidence": "low"},
        ],
    }
    resp = _FakeResponse(payload, input_tokens=1000, output_tokens=200)
    hooks = _wire(monkeypatch, fake_store, resp)

    result = extract.run_collect(_job(), "claimant-1")

    assert result["quotes_minted"] == 1
    assert result["quotes_rejected"] == 2
    assert result["findings_minted"] == 1
    assert result["findings_rejected"] == 1
    assert result["review_chars"] == len(extract.extract_review_text(RAW_HTML))

    # The one minted quote was evidenced against the REVIEW text, never the page text.
    assert len(fake_store.voc_quotes) == 1
    minted_quote = fake_store.voc_quotes[0]
    assert minted_quote["quote"] == "incredibly comfortable and true to size"
    assert minted_quote["connector"] == "web.fetch"
    assert minted_quote["raw_content"] == extract.extract_review_text(RAW_HTML)
    assert "Buy 2 get 1 free" not in minted_quote["raw_content"]
    assert minted_quote["competitor_id"] == "comp-1"
    assert minted_quote["store_id"] == "mv"
    # An exact literal match exists, so a source span was attached.
    assert minted_quote["source_start"] is not None
    assert minted_quote["source_end"] is not None

    # The one minted finding was evidenced against the CANONICAL (whole-page) text. Its
    # `detail` was blank, so the verbatim `evidence` was folded into `detail`; `evidence`
    # itself is never sent to the RPC (not part of the registered site_fact v1 payload).
    assert len(fake_store.findings) == 1
    minted_finding = fake_store.findings[0]
    assert minted_finding["payload"] == {
        "fact_type": "pricing", "statement": "Buy 2 get 1 free", "detail": "today only!",
    }
    assert minted_finding["raw_content"] == CANONICAL_TEXT
    assert minted_finding["project_id"] == "proj-1"
    assert minted_finding["store_id"] == "mv"

    # budget.settle called with report_usage=False (the Anthropic call already reported
    # its own real token counts via usage_reporter.spend -- never double-counted).
    assert len(hooks["settle_calls"]) == 1
    settle_call = hooks["settle_calls"][0]
    assert settle_call["report_usage"] is False
    assert settle_call["reserved_est"] == 25
    # Per-CLAIM reserve/settle ref: the claimant token is minted fresh per claim, so a failed
    # collect (which settles 0 under its own ref) never poisons the next claim's reservation.
    assert settle_call["ref"] == "collect:cap-1:text@v2:claimant-1"
    assert settle_call["actual_cents"] == extract._actual_cents(1000, 200)

    assert len(hooks["usage_calls"]) == 1
    assert hooks["usage_calls"][0]["input_tokens"] == 1000
    assert hooks["usage_calls"][0]["output_tokens"] == 200


def test_run_collect_already_collected_short_circuits_before_any_work(monkeypatch):
    """A capture whose collect already reached 'done' is never re-processed: no object download,
    no reservation, no Anthropic call, no mint. This is the retry-idempotency backstop -- a job
    re-run (reaper re-claim, or a duplicate enqueue) after a genuine completion does zero work
    and spends nothing, instead of re-paying to re-mint duplicate evidence."""
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
        collect_done=True,
    )
    resp = _FakeResponse({"quotes": [], "findings": []})
    hooks = _wire(monkeypatch, fake_store, resp)

    result = extract.run_collect(_job(), "claimant-1")

    assert result["skipped"] == "already_collected"
    assert result["quotes_minted"] == 0
    assert result["findings_minted"] == 0
    # Nothing downstream of the 'done' check ran: no spend, no model call, no writes.
    assert hooks["reserve_calls"] == []
    assert hooks["settle_calls"] == []
    assert hooks["anthropic_calls"] == []
    assert hooks["usage_calls"] == []
    assert fake_store.voc_quotes == []
    assert fake_store.findings == []


def test_run_collect_reservation_ref_is_per_claim_not_stable(monkeypatch):
    """The reserve/settle ref carries the fresh-per-claim `claimant` token, so a FAILED collect
    (which settles 0 under its own claimant's ref) can never poison a later claim's reservation:
    the retry's distinct claimant yields a distinct ref that reserve() treats as new work. This
    is the mechanism that fixes the 'stable ref permanently skips the retry' failure mode."""
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    resp = _FakeResponse({"quotes": [], "findings": []}, input_tokens=10, output_tokens=5)
    hooks = _wire(monkeypatch, fake_store, resp)

    extract.run_collect(_job(), "claimant-RETRY-2")

    assert len(hooks["reserve_calls"]) == 1
    # reserve_calls entries are (job, ref, claimant, connector).
    assert hooks["reserve_calls"][0][1] == "collect:cap-1:text@v2:claimant-RETRY-2"
    assert hooks["reserve_calls"][0][2] == "claimant-RETRY-2"
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["ref"] == "collect:cap-1:text@v2:claimant-RETRY-2"


def test_run_collect_empty_review_text_mints_zero_quotes_even_if_model_returns_some(monkeypatch):
    raw_html_no_widget = "<html><body><p>Just marketing copy, no reviews here.</p></body></html>"
    fake_store = _FakeStore(
        capture=_capture(),
        objects={
            "captures/canonsha.txt": canonical_text(raw_html_no_widget).encode("utf-8"),
            "captures/rawsha.html": raw_html_no_widget.encode("utf-8"),
        },
    )
    payload = {
        "quotes": [
            {"quote": "Just marketing copy, no reviews here.", "type": "desire",
             "theme": "x", "product_area": "y", "confidence": "high"},
        ],
        "findings": [],
    }
    resp = _FakeResponse(payload)
    _wire(monkeypatch, fake_store, resp)

    result = extract.run_collect(_job(), "claimant-1")

    assert result["review_chars"] == 0
    assert result["quotes_minted"] == 0
    assert result["quotes_rejected"] == 1
    assert fake_store.voc_quotes == []


def test_run_collect_red_team_poisoned_brand_copy_quote_never_reaches_the_mint_rpc(monkeypatch):
    """RED-TEAM: a poisoned extraction where the model labels PAGE (brand/marketing)
    text as a customer-voice quote -- text that is real, but NOT present in the review
    widget's own text. This must be dropped by the Python pre-check BEFORE ever calling
    store.create_voc_quote -- proving the customer-voice boundary is structural (S2-5),
    not merely "trust the RPC to catch it"."""
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    poisoned_quote = "Buy 2 get 1 free, today only!"  # real text, but it's PAGE copy, not review copy
    payload = {
        "quotes": [
            {"quote": poisoned_quote, "type": "desire", "theme": "offer",
             "product_area": "bras", "confidence": "high"},
        ],
        "findings": [],
    }
    resp = _FakeResponse(payload)
    _wire(monkeypatch, fake_store, resp)

    result = extract.run_collect(_job(), "claimant-1")

    assert result["quotes_minted"] == 0
    assert result["quotes_rejected"] == 1
    assert fake_store.voc_quotes == []  # the mint RPC was never even called for it


def test_run_collect_rpc_rejection_of_a_pre_check_passed_quote_is_caught_and_dropped(monkeypatch):
    """The RPC's own A-1 check is the real backstop: even a quote that PASSES this
    module's Python pre-check must still be droppable (never crash the job) if the
    deployed RPC itself rejects it."""
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
        raise_on_quote={"incredibly comfortable and true to size"},
    )
    payload = {
        "quotes": [
            {"quote": "incredibly comfortable and true to size", "type": "desire",
             "theme": "comfort", "product_area": "bras", "confidence": "high"},
        ],
        "findings": [],
    }
    resp = _FakeResponse(payload)
    _wire(monkeypatch, fake_store, resp)

    result = extract.run_collect(_job(), "claimant-1")

    assert result["quotes_minted"] == 0
    assert result["quotes_rejected"] == 1


def test_run_collect_prompt_truncates_oversized_input(monkeypatch):
    huge_review_html = (
        "<div class='jdgm-rev-widg'><div class='jdgm-rev__body'>" + ("x" * 50000) + "</div></div>"
    )
    fake_store = _FakeStore(
        capture=_capture(),
        objects={
            "captures/canonsha.txt": canonical_text(huge_review_html).encode("utf-8"),
            "captures/rawsha.html": huge_review_html.encode("utf-8"),
        },
    )
    resp = _FakeResponse({"quotes": [], "findings": []})
    hooks = _wire(monkeypatch, fake_store, resp)

    result = extract.run_collect(_job(), "claimant-1")

    # review_chars reports the FULL untruncated review text length...
    assert result["review_chars"] == 50000
    # ...but the actual prompt sent to the model is capped.
    prompt = hooks["anthropic_calls"][0]["messages"][0]["content"]
    assert len(prompt) < 50000 + 20000  # nowhere near the full 50k review text length
    assert "x" * extract._MAX_INPUT_CHARS in prompt


def test_run_collect_source_url_falls_back_to_capture_url(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(source_url=None, url="https://competitor.com/fallback"),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    payload = {
        "quotes": [
            {"quote": "incredibly comfortable and true to size", "type": "desire",
             "theme": "comfort", "product_area": "bras", "confidence": "high"},
        ],
        "findings": [],
    }
    resp = _FakeResponse(payload)
    _wire(monkeypatch, fake_store, resp)

    extract.run_collect(_job(), "claimant-1")

    assert fake_store.voc_quotes[0]["source_url"] == "https://competitor.com/fallback"


# ===========================================================================
# FIX 4 -- model-correct pricing + worst-case-reservation guard.
# ===========================================================================

def test_run_collect_under_reserve_guard_raises_for_expensive_model(monkeypatch):
    """If `settings.model` (RESEARCH_MODEL) is configured to a model whose worst-case
    cost exceeds the 'collect' price card's reservation, run_collect must release the
    reservation and fail loud -- never silently proceed on an under-priced call."""
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    monkeypatch.setenv("RESEARCH_MODEL", "claude-opus-5")
    resp = _FakeResponse({"quotes": [], "findings": []})
    hooks = _wire(monkeypatch, fake_store, resp)

    with pytest.raises(ValueError, match="under-reserves"):
        extract.run_collect(_job(), "claimant-1")

    assert hooks["anthropic_calls"] == []
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 0


def test_run_collect_under_reserve_guard_raises_for_unpriced_model(monkeypatch):
    """A model with NO configured USD/MTok rate at all is treated the same as an
    under-reserved card: release + raise loud, never proceed on a guessed rate."""
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    monkeypatch.setenv("RESEARCH_MODEL", "some-unpriced-model")
    resp = _FakeResponse({"quotes": [], "findings": []})
    hooks = _wire(monkeypatch, fake_store, resp)

    with pytest.raises(ValueError, match="under-reserves"):
        extract.run_collect(_job(), "claimant-1")

    assert hooks["anthropic_calls"] == []
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 0


def test_actual_cents_uses_configured_model_rate_not_hardcoded_sonnet(monkeypatch):
    """`_actual_cents` must price at `settings.model`'s OWN rate, not a rate hardcoded for
    one specific model -- a differently-configured (but priced) model settles its true
    cost, not Sonnet's."""
    monkeypatch.setenv("RESEARCH_MODEL", "claude-haiku-4-5")
    # 1,000,000 input @ $0.8/MTok + 1,000,000 output @ $4/MTok = $4.8 = 480 cents.
    assert extract._actual_cents(1_000_000, 1_000_000) == 480


# ===========================================================================
# FIX 6 -- lease fenced immediately before EACH mint call, not once for the block.
# ===========================================================================

def test_run_collect_per_mint_lease_loss_stops_further_mints(monkeypatch):
    """Losing the lease between two quote candidates must stop immediately: the first
    quote (checked and minted before the loss) is minted, the second (checked after) is
    never even attempted, and findings are never reached at all."""
    raw_html_two = (
        "<html><body>"
        "<div class='jdgm-rev-widg'>"
        "<div class='jdgm-rev__body'>incredibly comfortable and true to size</div>"
        "<div class='jdgm-rev__body'>perfect for everyday wear</div>"
        "</div>"
        "</body></html>"
    )
    canonical = canonical_text(raw_html_two)
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": canonical.encode("utf-8"),
                  "captures/rawsha.html": raw_html_two.encode("utf-8")},
    )
    payload = {
        "quotes": [
            {"quote": "incredibly comfortable and true to size", "type": "desire",
             "theme": "comfort", "product_area": "bras", "confidence": "high"},
            {"quote": "perfect for everyday wear", "type": "desire",
             "theme": "comfort", "product_area": "bras", "confidence": "high"},
        ],
        "findings": [
            {"fact_type": "pricing", "statement": "irrelevant", "detail": "",
             "evidence": "irrelevant", "confidence": "low"},
        ],
    }
    resp = _FakeResponse(payload)
    # pre-reserve True, pre-paid-call True, mint-quote-1 True, mint-quote-2 False.
    hooks = _wire(monkeypatch, fake_store, resp, lease_values=[True, True, True, False])

    with pytest.raises(RuntimeError, match="before minting"):
        extract.run_collect(_job(), "claimant-1")

    assert len(fake_store.voc_quotes) == 1
    assert fake_store.voc_quotes[0]["quote"] == "incredibly comfortable and true to size"
    assert fake_store.findings == []
    # The spend was already honestly settled before minting began.
    assert len(hooks["settle_calls"]) == 1


# ===========================================================================
# FIX 7 -- deterministic grounding gate: a finding's `evidence` must be a
# normalized-substring of the canonical page text, or it is dropped.
# ===========================================================================

def test_run_collect_finding_with_evidence_not_in_page_is_dropped(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    payload = {
        "quotes": [],
        "findings": [
            {"fact_type": "pricing", "statement": "Buy 2 get 1 free",
             "evidence": "this sentence never appears anywhere on the page",
             "detail": "", "confidence": "medium"},
        ],
    }
    resp = _FakeResponse(payload)
    _wire(monkeypatch, fake_store, resp)

    result = extract.run_collect(_job(), "claimant-1")

    assert result["findings_minted"] == 0
    assert result["findings_rejected"] == 1
    assert fake_store.findings == []


def test_run_collect_finding_with_blank_evidence_is_dropped(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    payload = {
        "quotes": [],
        "findings": [
            {"fact_type": "pricing", "statement": "Buy 2 get 1 free",
             "evidence": "", "detail": "", "confidence": "medium"},
        ],
    }
    resp = _FakeResponse(payload)
    _wire(monkeypatch, fake_store, resp)

    result = extract.run_collect(_job(), "claimant-1")

    assert result["findings_minted"] == 0
    assert result["findings_rejected"] == 1
    assert fake_store.findings == []


def test_run_collect_finding_evidence_not_folded_when_detail_present(monkeypatch):
    """When the model DID supply a non-blank `detail`, the verbatim `evidence` is used
    only as the grounding gate and then discarded -- never appended to or overwriting the
    model's own `detail`."""
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    payload = {
        "quotes": [],
        "findings": [
            {"fact_type": "pricing", "statement": "Buy 2 get 1 free",
             "detail": "limited to the first 100 orders", "evidence": "today only!",
             "confidence": "medium"},
        ],
    }
    resp = _FakeResponse(payload)
    _wire(monkeypatch, fake_store, resp)

    extract.run_collect(_job(), "claimant-1")

    assert fake_store.findings[0]["payload"] == {
        "fact_type": "pricing", "statement": "Buy 2 get 1 free",
        "detail": "limited to the first 100 orders",
    }


# ===========================================================================
# FIX P2 -- an INFRA failure during a mint RPC must propagate, never be
# miscounted as a content rejection.
# ===========================================================================

def test_run_collect_infra_exception_during_quote_mint_propagates(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
        infra_raise_on_quote={"incredibly comfortable and true to size"},
    )
    payload = {
        "quotes": [
            {"quote": "incredibly comfortable and true to size", "type": "desire",
             "theme": "comfort", "product_area": "bras", "confidence": "high"},
        ],
        "findings": [],
    }
    resp = _FakeResponse(payload)
    _wire(monkeypatch, fake_store, resp)

    with pytest.raises(RuntimeError, match="connection reset"):
        extract.run_collect(_job(), "claimant-1")


def test_run_collect_infra_exception_during_finding_mint_propagates(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
        infra_raise_on_finding={"Buy 2 get 1 free"},
    )
    payload = {
        "quotes": [],
        "findings": [
            {"fact_type": "pricing", "statement": "Buy 2 get 1 free",
             "evidence": "Buy 2 get 1 free", "detail": "", "confidence": "medium"},
        ],
    }
    resp = _FakeResponse(payload)
    _wire(monkeypatch, fake_store, resp)

    with pytest.raises(RuntimeError, match="connection reset"):
        extract.run_collect(_job(), "claimant-1")


def test_is_content_rejection_true_for_rpc_validation_messages():
    assert extract._is_content_rejection(RuntimeError("p_quote is not verbatim-present"))
    assert extract._is_content_rejection(RuntimeError("connector is not an enabled connector"))
    assert extract._is_content_rejection(RuntimeError("source span mismatch"))
    assert extract._is_content_rejection(RuntimeError("not a registered finding kind/version"))
    assert extract._is_content_rejection(RuntimeError("p_statement is required"))


def test_is_content_rejection_false_for_infra_messages():
    assert not extract._is_content_rejection(RuntimeError("connection reset by peer"))
    assert not extract._is_content_rejection(TimeoutError("upstream timed out"))


# ===========================================================================
# FIX 5 -- settle retry: a transient failure on the SUCCESS-PATH settle must
# not immediately orphan the reservation.
# ===========================================================================

def test_run_collect_settle_retries_on_transient_failure_then_succeeds(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    resp = _FakeResponse({"quotes": [], "findings": []})
    hooks = _wire(monkeypatch, fake_store, resp)

    attempts = []

    def _flaky_settle(job, ref, actual_cents, claimant, reserved_est, report_usage=True):
        attempts.append(actual_cents)
        if len(attempts) < 3:
            raise RuntimeError("transient DB blip")
        hooks["settle_calls"].append({
            "job": job, "ref": ref, "actual_cents": actual_cents, "claimant": claimant,
            "reserved_est": reserved_est, "report_usage": report_usage,
        })

    monkeypatch.setattr(extract.budget, "settle", _flaky_settle)
    monkeypatch.setattr(extract.time, "sleep", lambda _seconds: None)

    result = extract.run_collect(_job(), "claimant-1")

    assert len(attempts) == 3
    assert result["quotes_minted"] == 0
    assert len(hooks["settle_calls"]) == 1


def test_run_collect_settle_exhausts_retries_logs_and_raises(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
                  "captures/rawsha.html": RAW_HTML.encode("utf-8")},
    )
    resp = _FakeResponse({"quotes": [], "findings": []})
    _wire(monkeypatch, fake_store, resp)

    attempts = []

    def _always_fails(job, ref, actual_cents, claimant, reserved_est, report_usage=True):
        attempts.append(actual_cents)
        raise RuntimeError("permanent DB outage")

    monkeypatch.setattr(extract.budget, "settle", _always_fails)
    monkeypatch.setattr(extract.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="permanent DB outage"):
        extract.run_collect(_job(), "claimant-1")

    assert len(attempts) == extract._SETTLE_MAX_ATTEMPTS
