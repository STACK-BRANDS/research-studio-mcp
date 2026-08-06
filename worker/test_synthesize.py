"""Tests for the `synthesize` job (P4 PRODUCER of the deep-research plan, v2.1):
worker/synthesize.py's `run_synthesize` + its pure helpers, and the worker/store.py
wrappers it depends on (get_publishable_evidence, compute_theme_rollups,
get_theme_rollup_batch, rs_create_synthesis).

No network, no real Supabase, no real Anthropic call: every collaborator
(`synthesize.store`, `synthesize.jobs.assert_lease`, `synthesize.budget.reserve`/
`settle`, `synthesize.Anthropic`, `synthesize.usage_reporter`) is monkeypatched directly
on the module objects that use them -- the exact same pattern `worker/test_verify.py`
and `worker/test_extract.py` use. The deployed RPCs themselves (`rs_create_synthesis`,
`rs_compute_theme_rollups`, migrations 157/170) are NOT re-tested here -- those
migrations own that; this file tests only the Python driving them.
"""
import json

import pytest

from worker import store, synthesize
from worker.budget import ReserveResult


# ===========================================================================
# worker/store.py -- the two new P4 PRODUCER wrappers, tested directly against fake
# Supabase clients (query-builder for get_publishable_evidence, RPC for
# rs_create_synthesis).
# ===========================================================================

class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self.filters = {}
        self.limit_value = None

    def select(self, *_a, **_k):
        return self

    def eq(self, field, value):
        self.filters[field] = value
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def execute(self):
        matching = [r for r in self._rows if all(r.get(k) == v for k, v in self.filters.items())]
        if self.limit_value is not None:
            matching = matching[: self.limit_value]
        return _FakeResult(matching)


class _FakeTableClient:
    def __init__(self, rows):
        self._rows = rows
        self.queries = []

    def table(self, _name):
        q = _FakeQuery(self._rows)
        self.queries.append(q)
        return q


def test_get_publishable_evidence_returns_rows_for_store(monkeypatch):
    rows = [
        {"evidence_id": "quote-1", "evidence_kind": "voc", "store_id": "mv", "summary": "loves the fit"},
        {"evidence_id": "finding-1", "evidence_kind": "finding", "store_id": "mv", "summary": "free shipping"},
        {"evidence_id": "quote-2", "evidence_kind": "voc", "store_id": "other", "summary": "irrelevant"},
    ]
    monkeypatch.setattr(store, "_client", lambda: _FakeTableClient(rows))
    result = store.get_publishable_evidence("mv")
    assert [r["evidence_id"] for r in result] == ["quote-1", "finding-1"]


def test_get_publishable_evidence_applies_limit(monkeypatch):
    rows = [
        {"evidence_id": f"e-{i}", "evidence_kind": "voc", "store_id": "mv", "summary": "x"}
        for i in range(10)
    ]
    client = _FakeTableClient(rows)
    monkeypatch.setattr(store, "_client", lambda: client)
    result = store.get_publishable_evidence("mv", limit=3)
    assert len(result) == 3
    assert client.queries[0].limit_value == 3


def test_get_publishable_evidence_returns_empty_list_when_none(monkeypatch):
    monkeypatch.setattr(store, "_client", lambda: _FakeTableClient([]))
    assert store.get_publishable_evidence("mv") == []


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


def test_rs_create_synthesis_sends_expected_params_with_all_fields(monkeypatch):
    client = _FakeRpcClient(rpc_response="syn-id-1")
    monkeypatch.setattr(store, "_client", lambda: client)

    syn_id = store.rs_create_synthesis(
        store_id="mv", kind="pain_map", schema_version=1, title="Pain map",
        payload={"title": "Pain map", "pains": []}, evidence_refs=[{"table": "research_voc_quotes", "id": "q1"}],
        confidence="high", area="pdp", project_id="proj-1", origin="agent",
        thin_data=False, created_by="worker",
    )
    assert syn_id == "syn-id-1"
    assert client.rpcs[0].name == "rs_create_synthesis"
    assert client.rpcs[0].params == {
        "p_store_id": "mv", "p_kind": "pain_map", "p_schema_version": 1, "p_title": "Pain map",
        "p_payload": {"title": "Pain map", "pains": []},
        "p_evidence_refs": [{"table": "research_voc_quotes", "id": "q1"}],
        "p_confidence": "high", "p_origin": "agent", "p_thin_data": False,
        "p_area": "pdp", "p_project_id": "proj-1", "p_created_by": "worker",
    }


def test_rs_create_synthesis_omits_optional_params_when_none(monkeypatch):
    client = _FakeRpcClient(rpc_response="syn-id-2")
    monkeypatch.setattr(store, "_client", lambda: client)

    store.rs_create_synthesis(
        store_id="mv", kind="pain_map", schema_version=1, title="t",
        payload={"title": "t", "pains": []}, evidence_refs=[], confidence="low",
    )
    assert client.rpcs[0].params == {
        "p_store_id": "mv", "p_kind": "pain_map", "p_schema_version": 1, "p_title": "t",
        "p_payload": {"title": "t", "pains": []}, "p_evidence_refs": [],
        "p_confidence": "low", "p_origin": "agent", "p_thin_data": False,
    }


def test_rs_create_synthesis_propagates_rpc_exception(monkeypatch):
    client = _FakeRpcClient(raise_exc=RuntimeError("ref no longer publishable"))
    monkeypatch.setattr(store, "_client", lambda: client)
    with pytest.raises(RuntimeError, match="ref no longer publishable"):
        store.rs_create_synthesis(
            store_id="mv", kind="pain_map", schema_version=1, title="t",
            payload={}, evidence_refs=[], confidence="low",
        )


# ===========================================================================
# worker/store.py -- compute_theme_rollups / get_theme_rollup_batch (v2.1, migration
# 170 theme-rollup wiring), tested directly against fake Supabase clients.
# ===========================================================================

def test_compute_theme_rollups_sends_expected_params_and_returns_batch_id(monkeypatch):
    client = _FakeRpcClient(rpc_response="batch-1")
    monkeypatch.setattr(store, "_client", lambda: client)

    batch_id = store.compute_theme_rollups("mv", project_id="proj-1")

    assert batch_id == "batch-1"
    assert client.rpcs[0].name == "rs_compute_theme_rollups"
    assert client.rpcs[0].params == {"p_store_id": "mv", "p_project_id": "proj-1"}


def test_compute_theme_rollups_sends_explicit_none_project_id_when_omitted(monkeypatch):
    client = _FakeRpcClient(rpc_response="batch-2")
    monkeypatch.setattr(store, "_client", lambda: client)

    store.compute_theme_rollups("mv")

    assert client.rpcs[0].params == {"p_store_id": "mv", "p_project_id": None}


def test_compute_theme_rollups_propagates_rpc_exception(monkeypatch):
    client = _FakeRpcClient(raise_exc=RuntimeError("db exploded"))
    monkeypatch.setattr(store, "_client", lambda: client)
    with pytest.raises(RuntimeError, match="db exploded"):
        store.compute_theme_rollups("mv")


class _FakeMultiTableClient:
    """Routes `.table(name)` to a per-table fixed row list -- unlike `_FakeTableClient`
    above (which serves one row list regardless of table name), `get_theme_rollup_batch`
    reads from TWO distinct tables (`research_theme_rollup_batches` header,
    `research_theme_rollups` rows) in one call, so the fake must tell them apart."""

    def __init__(self, tables: dict):
        self._tables = tables
        self.queries = []

    def table(self, name):
        q = _FakeQuery(self._tables.get(name, []))
        self.queries.append((name, q))
        return q


def test_get_theme_rollup_batch_returns_header_and_rows(monkeypatch):
    header_row = {
        "batch_id": "batch-1", "basis": {"total_publishable_quotes": 12, "as_of": "2026-08-01T00:00:00Z"},
        "computed_at": "2026-08-01T00:00:00Z", "store_id": "mv", "project_id": None,
    }
    matching_row = {
        "theme": "fit", "quote_type": "complaint", "count": 5, "data_density": "normal",
        "member_quote_ids": ["q1", "q2"], "example_quote_ids": ["q1"], "batch_id": "batch-1",
    }
    other_batch_row = {
        "theme": "other", "quote_type": "praise", "count": 9, "data_density": "normal",
        "member_quote_ids": [], "example_quote_ids": [], "batch_id": "batch-OTHER",
    }
    client = _FakeMultiTableClient({
        "research_theme_rollup_batches": [header_row],
        "research_theme_rollups": [matching_row, other_batch_row],
    })
    monkeypatch.setattr(store, "_client", lambda: client)

    result = store.get_theme_rollup_batch("batch-1")

    assert result == {"header": header_row, "rows": [matching_row]}


def test_get_theme_rollup_batch_raises_when_header_missing(monkeypatch):
    client = _FakeMultiTableClient({
        "research_theme_rollup_batches": [], "research_theme_rollups": [],
    })
    monkeypatch.setattr(store, "_client", lambda: client)

    with pytest.raises(ValueError, match="batch_id"):
        store.get_theme_rollup_batch("missing-batch")


def test_get_theme_rollup_batch_returns_empty_rows_when_batch_has_none(monkeypatch):
    header_row = {
        "batch_id": "batch-empty", "basis": {"total_publishable_quotes": 0, "as_of": None},
        "computed_at": "2026-08-01T00:00:00Z", "store_id": "mv", "project_id": None,
    }
    client = _FakeMultiTableClient({
        "research_theme_rollup_batches": [header_row], "research_theme_rollups": [],
    })
    monkeypatch.setattr(store, "_client", lambda: client)

    result = store.get_theme_rollup_batch("batch-empty")

    assert result == {"header": header_row, "rows": []}


# ===========================================================================
# worker/synthesize.py -- run_synthesize, stubbed Anthropic + store.
# ===========================================================================

class _FakeUsage:
    def __init__(self, input_tokens=200, output_tokens=80):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, payload, input_tokens=200, output_tokens=80):
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


class _RaisingAnthropicClient:
    """Simulates a provider/network failure mid-call."""

    def __init__(self, exc):
        self._exc = exc

    @property
    def messages(self):
        return self

    def stream(self, **kwargs):
        raise self._exc


def _default_rollup_header(batch_id):
    # Ample by default (total_publishable_quotes >= _THIN_DATA_MIN_EVIDENCE, no thin
    # rows) so every test that doesn't care about thin_data gets thin_data=False,
    # matching this module's pre-v2.1 default expectations.
    return {
        "batch_id": batch_id,
        "basis": {"total_publishable_quotes": 20, "as_of": "2026-01-01T00:00:00Z"},
        "computed_at": "2026-01-01T00:00:00Z", "store_id": "mv", "project_id": None,
    }


def _default_rollup_rows():
    return [
        {"theme": "fit", "quote_type": "complaint", "count": 5, "data_density": "normal",
         "member_quote_ids": ["quote-0", "quote-1"], "example_quote_ids": ["quote-0"]},
    ]


class _FakeSynthesizeStore:
    """Records every call; `get_publishable_evidence` returns a fixed evidence set,
    `compute_theme_rollups`/`get_theme_rollup_batch` return a fixed (or raising) rollup
    batch, `rs_create_synthesis` records its call and either returns a fixed id or
    raises."""

    def __init__(
        self, evidence=None, create_raises=None, create_returns="syn-1",
        rollup_batch_id="rollup-batch-1", rollup_header=None, rollup_rows=None,
        compute_rollup_raises=None, get_rollup_raises=None,
    ):
        self.evidence = evidence if evidence is not None else []
        self.create_raises = create_raises
        self.create_returns = create_returns
        self.get_publishable_evidence_calls = []
        self.create_synthesis_calls = []

        self.rollup_batch_id = rollup_batch_id
        self.rollup_header = (
            rollup_header if rollup_header is not None else _default_rollup_header(rollup_batch_id)
        )
        self.rollup_rows = rollup_rows if rollup_rows is not None else _default_rollup_rows()
        self.compute_rollup_raises = compute_rollup_raises
        self.get_rollup_raises = get_rollup_raises
        self.compute_theme_rollups_calls = []
        self.get_theme_rollup_batch_calls = []

    def get_publishable_evidence(self, store_id, limit=None):
        self.get_publishable_evidence_calls.append((store_id, limit))
        return list(self.evidence)

    def compute_theme_rollups(self, store_id, project_id=None):
        self.compute_theme_rollups_calls.append((store_id, project_id))
        if self.compute_rollup_raises is not None:
            raise self.compute_rollup_raises
        return self.rollup_batch_id

    def get_theme_rollup_batch(self, batch_id):
        self.get_theme_rollup_batch_calls.append(batch_id)
        if self.get_rollup_raises is not None:
            raise self.get_rollup_raises
        return {"header": self.rollup_header, "rows": list(self.rollup_rows)}

    def rs_create_synthesis(self, **kwargs):
        self.create_synthesis_calls.append(kwargs)
        if self.create_raises is not None:
            raise self.create_raises
        return self.create_returns


def _evidence_row(evidence_id, evidence_kind="voc", summary="some VoC text"):
    return {"evidence_id": evidence_id, "evidence_kind": evidence_kind, "store_id": "mv", "summary": summary}


def _publishable_set(n=10):
    return [_evidence_row(f"quote-{i}") for i in range(n)]


def _wire(monkeypatch, fake_store, resp=None, lease_values=True, reserve_result=None, anthropic_client=None):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(synthesize, "store", fake_store)

    if isinstance(lease_values, list):
        it = iter(lease_values)
        monkeypatch.setattr(synthesize.jobs, "assert_lease", lambda job_id, claimant: next(it))
    else:
        monkeypatch.setattr(synthesize.jobs, "assert_lease", lambda job_id, claimant: lease_values)

    if reserve_result is None:
        # 75 cents -- the deployed 'synthesize' price card's own default ('*': 75), and
        # comfortably covers _worst_case_cents(settings.model) at claude-sonnet-5's rate.
        reserve_result = ReserveResult(ok=True, reserved_est_cents=75, project_scoped=True)
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

    monkeypatch.setattr(synthesize.budget, "reserve", _fake_reserve)
    monkeypatch.setattr(synthesize.budget, "settle", _fake_settle)

    anthropic_calls = []
    if anthropic_client is not None:
        monkeypatch.setattr(synthesize, "Anthropic", lambda api_key: anthropic_client)
    else:
        monkeypatch.setattr(
            synthesize, "Anthropic", lambda api_key: _FakeAnthropicClient(resp, anthropic_calls)
        )

    usage_calls = []
    monkeypatch.setattr(synthesize.usage_reporter, "spend", lambda **kw: usage_calls.append(kw))

    return {
        "reserve_calls": reserve_calls, "settle_calls": settle_calls,
        "anthropic_calls": anthropic_calls, "usage_calls": usage_calls,
    }


def _job(**overrides):
    job = {
        "id": "job-1", "job_kind": "synthesize", "project_id": "proj-1",
        "params": {"store_id": "mv", "kind": "pain_map", "project_id": "proj-1", "area": "pdp"},
    }
    job.update(overrides)
    return job


def _pain_payload(pains):
    return {"title": "MV VoC pain map", "pains": pains}


def _pain(theme="fit", summary="customers say sizing runs small", refs=None):
    return {"theme": theme, "summary": summary, "evidence_refs": refs or []}


# ===========================================================================
# Happy path.
# ===========================================================================

def test_run_synthesize_happy_path_creates_synthesis_with_cited_refs(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence, create_returns="syn-42")
    resp = _FakeResponse(_pain_payload([
        _pain(refs=[{"kind": "voc", "id": "quote-0"}, {"kind": "voc", "id": "quote-1"}]),
    ]))
    hooks = _wire(monkeypatch, fake_store, resp)

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "done"
    assert error is None
    assert cost_cents == synthesize._actual_cents(200, 80)

    # Exactly one reserve, one settle at the ACTUAL cost.
    assert len(hooks["reserve_calls"]) == 1
    assert len(hooks["settle_calls"]) == 1
    settle = hooks["settle_calls"][0]
    assert settle["actual_cents"] == cost_cents
    assert settle["reserved_est"] == 75
    assert settle["report_usage"] is False

    # rs_create_synthesis called once with the cited subset as evidence_refs.
    assert len(fake_store.create_synthesis_calls) == 1
    call = fake_store.create_synthesis_calls[0]
    assert call["store_id"] == "mv"
    assert call["kind"] == "pain_map"
    assert call["schema_version"] == 2  # bumped 1 -> 2, v2.1 rollup block.
    assert call["confidence"] in {"high", "medium", "low"}
    assert call["origin"] == "agent"
    # thin_data now comes from the fake store's default rollup fixture (ample:
    # total_publishable_quotes=20, no thin rows), NOT from len(evidence).
    assert call["thin_data"] is False
    assert call["area"] == "pdp"
    assert call["project_id"] == "proj-1"
    assert set(call["evidence_refs"][0]) == {"table", "id"}
    assert {r["id"] for r in call["evidence_refs"]} == {"quote-0", "quote-1"}
    assert all(r["table"] == "research_voc_quotes" for r in call["evidence_refs"])
    assert call["payload"]["pains"][0]["evidence_refs"] == [
        {"kind": "voc", "id": "quote-0"}, {"kind": "voc", "id": "quote-1"},
    ]
    assert call["payload"]["rollup"] == {
        "batch_id": "rollup-batch-1", "as_of": "2026-01-01T00:00:00Z",
        "total_publishable_quotes": 20,
        "themes": [
            {"theme": "fit", "quote_type": "complaint", "count": 5, "data_density": "normal"},
        ],
    }

    assert len(hooks["anthropic_calls"]) == 1
    assert len(hooks["usage_calls"]) == 1
    assert hooks["usage_calls"][0]["input_tokens"] == 200
    assert hooks["usage_calls"][0]["output_tokens"] == 80
    assert hooks["usage_calls"][0]["meta"]["rollup_batch_id"] == "rollup-batch-1"


def test_run_synthesize_maps_finding_kind_to_research_findings_table(monkeypatch):
    evidence = [_evidence_row("finding-1", evidence_kind="finding")]
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    resp = _FakeResponse(_pain_payload([_pain(refs=[{"kind": "finding", "id": "finding-1"}])]))
    _wire(monkeypatch, fake_store, resp)

    status, _cost, _error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "done"
    call = fake_store.create_synthesis_calls[0]
    assert call["evidence_refs"] == [{"table": "research_findings", "id": "finding-1"}]


# ===========================================================================
# Missing required params -- failed, NO reserve, before any spend.
# ===========================================================================

def test_run_synthesize_missing_store_id_fails_without_reserve(monkeypatch):
    fake_store = _FakeSynthesizeStore(evidence=_publishable_set(10))
    hooks = _wire(monkeypatch, fake_store, resp=None)

    job = _job(params={"kind": "pain_map"})
    status, cost_cents, error = synthesize.run_synthesize(job, "claimant-1")

    assert status == "failed"
    assert cost_cents == 0
    assert "store_id" in error
    assert error.startswith("run_synthesize:")
    assert hooks["reserve_calls"] == []
    assert fake_store.get_publishable_evidence_calls == []
    assert fake_store.create_synthesis_calls == []


def test_run_synthesize_missing_kind_fails_without_reserve(monkeypatch):
    fake_store = _FakeSynthesizeStore(evidence=_publishable_set(10))
    hooks = _wire(monkeypatch, fake_store, resp=None)

    job = _job(params={"store_id": "mv"})
    status, cost_cents, error = synthesize.run_synthesize(job, "claimant-1")

    assert status == "failed"
    assert cost_cents == 0
    assert "kind" in error
    assert hooks["reserve_calls"] == []
    assert fake_store.create_synthesis_calls == []


# ===========================================================================
# Empty publishable set -- failed, NO reserve, NO rs_create_synthesis.
# ===========================================================================

def test_run_synthesize_empty_publishable_set_fails_without_spend(monkeypatch):
    fake_store = _FakeSynthesizeStore(evidence=[])
    hooks = _wire(monkeypatch, fake_store, resp=None)

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "failed"
    assert cost_cents == 0
    assert "no publishable evidence" in error
    assert hooks["reserve_calls"] == []
    assert hooks["anthropic_calls"] == []
    assert fake_store.create_synthesis_calls == []
    # The rollup compute never runs on an empty evidence set -- the early return above
    # happens BEFORE step 3 in the docstring's order of operations.
    assert fake_store.compute_theme_rollups_calls == []
    assert fake_store.get_theme_rollup_batch_calls == []


# ===========================================================================
# Model cites an id NOT in the publishable set -- dropped; remaining valid refs still
# mint; all-hallucinated -> failed with the settled cost, no mint.
# ===========================================================================

def test_run_synthesize_drops_hallucinated_citation_keeps_valid_pain(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    resp = _FakeResponse(_pain_payload([
        _pain(theme="fit", refs=[
            {"kind": "voc", "id": "quote-0"},
            {"kind": "voc", "id": "quote-NOT-REAL"},  # hallucinated -- must be dropped.
        ]),
    ]))
    hooks = _wire(monkeypatch, fake_store, resp)

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "done"
    assert error is None
    call = fake_store.create_synthesis_calls[0]
    assert call["evidence_refs"] == [{"table": "research_voc_quotes", "id": "quote-0"}]
    assert call["payload"]["pains"][0]["evidence_refs"] == [{"kind": "voc", "id": "quote-0"}]
    # The hallucinated ref never reaches the RPC in any form.
    assert "quote-NOT-REAL" not in json.dumps(call)
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == cost_cents


def test_run_synthesize_drops_pain_with_only_hallucinated_refs_keeps_other_pain(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    resp = _FakeResponse(_pain_payload([
        _pain(theme="fabricated", refs=[{"kind": "voc", "id": "quote-GHOST"}]),
        _pain(theme="real", refs=[{"kind": "voc", "id": "quote-2"}]),
    ]))
    _wire(monkeypatch, fake_store, resp)

    status, _cost, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "done"
    assert error is None
    call = fake_store.create_synthesis_calls[0]
    themes = [p["theme"] for p in call["payload"]["pains"]]
    assert themes == ["real"]


def test_run_synthesize_all_citations_hallucinated_fails_with_settled_cost_no_mint(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    resp = _FakeResponse(_pain_payload([
        _pain(refs=[{"kind": "voc", "id": "quote-FAKE-1"}]),
        _pain(refs=[{"kind": "finding", "id": "finding-FAKE-2"}]),
    ]))
    hooks = _wire(monkeypatch, fake_store, resp)

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "failed"
    assert "no publishable evidence" in error
    assert cost_cents == synthesize._actual_cents(200, 80)
    # Spend already settled (real tokens were billed) -- never orphaned.
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == cost_cents
    # rs_create_synthesis is never called for an uncited synthesis.
    assert fake_store.create_synthesis_calls == []


def test_run_synthesize_model_returns_zero_pains_fails_no_mint(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    resp = _FakeResponse(_pain_payload([]))
    _wire(monkeypatch, fake_store, resp)

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "failed"
    assert "no publishable evidence" in error
    assert cost_cents == synthesize._actual_cents(200, 80)
    assert fake_store.create_synthesis_calls == []


# ===========================================================================
# rs_create_synthesis raises (a ref went refuted mid-flight) -- reservation already
# settled, failed with the settled cost.
# ===========================================================================

def test_run_synthesize_rs_create_synthesis_rejection_fails_with_settled_cost(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(
        evidence=evidence, create_raises=RuntimeError("evidence ref no longer publishable"),
    )
    resp = _FakeResponse(_pain_payload([_pain(refs=[{"kind": "voc", "id": "quote-0"}])]))
    hooks = _wire(monkeypatch, fake_store, resp)

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "failed"
    assert "rs_create_synthesis rejected refs" in error
    assert "evidence ref no longer publishable" in error
    assert cost_cents == synthesize._actual_cents(200, 80)
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == cost_cents


# ===========================================================================
# Reserve 'skip' replay -- settles at ceiling, returns without a second paid call.
# ===========================================================================

def test_run_synthesize_reserve_skip_replay_settles_at_ceiling_no_paid_call(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    skip_result = ReserveResult(ok=False, reserved_est_cents=75, project_scoped=True)
    hooks = _wire(monkeypatch, fake_store, resp=None, reserve_result=skip_result)

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "failed"
    assert cost_cents == 75
    assert "reserve skip replay" in error
    assert hooks["anthropic_calls"] == []
    assert len(hooks["settle_calls"]) == 1
    settle = hooks["settle_calls"][0]
    assert settle["actual_cents"] == 75
    assert settle["reserved_est"] == 75
    assert settle["report_usage"] is False
    assert fake_store.create_synthesis_calls == []


# ===========================================================================
# Malformed provider response -- reservation settled at worst case, failed.
# ===========================================================================

def test_run_synthesize_malformed_json_response_fails_settles_worst_case(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)

    class _BadResponse:
        def __init__(self):
            self.content = [_FakeBlock("not json at all")]
            self.usage = _FakeUsage()

    class _BadClient:
        @property
        def messages(self):
            return self

        def stream(self, **kwargs):
            return _FakeStreamCtx(_BadResponse())

    hooks = _wire(monkeypatch, fake_store, anthropic_client=_BadClient())

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "failed"
    assert "not valid JSON" in error
    # Real tokens were billed for a response that came back at all -- settled at ACTUAL
    # (the malformed-JSON check runs AFTER settlement, same ordering as worker.verify's
    # malformed_response path), not the worst case.
    assert cost_cents == synthesize._actual_cents(200, 80)
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == cost_cents
    assert fake_store.create_synthesis_calls == []


def test_run_synthesize_malformed_payload_shape_fails(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    # Valid JSON, wrong shape: "pains" entries missing "evidence_refs".
    resp = _FakeResponse({"title": "t", "pains": [{"theme": "x", "summary": "y"}]})
    hooks = _wire(monkeypatch, fake_store, resp)

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "failed"
    assert "unexpected shape" in error
    assert cost_cents == synthesize._actual_cents(200, 80)
    assert len(hooks["settle_calls"]) == 1
    assert fake_store.create_synthesis_calls == []


def test_run_synthesize_incomplete_provider_response_settles_worst_case(monkeypatch):
    """No `.usage` at all -- must not crash on `resp.usage.input_tokens`, must settle
    at the WORST CASE (reserved_est), never orphaned."""
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)

    class _NoUsageResponse:
        def __init__(self):
            self.content = [_FakeBlock(json.dumps(_pain_payload([_pain(refs=[{"kind": "voc", "id": "quote-0"}])])))]

    class _IncompleteClient:
        @property
        def messages(self):
            return self

        def stream(self, **kwargs):
            return _FakeStreamCtx(_NoUsageResponse())

    hooks = _wire(monkeypatch, fake_store, anthropic_client=_IncompleteClient())

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "failed"
    assert "malformed provider response" in error
    assert cost_cents == 75  # reserved_est, the worst case.
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 75
    assert hooks["usage_calls"] == []
    assert fake_store.create_synthesis_calls == []


def test_run_synthesize_provider_error_settles_worst_case(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    hooks = _wire(
        monkeypatch, fake_store,
        anthropic_client=_RaisingAnthropicClient(RuntimeError("connection reset by peer")),
    )

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "failed"
    assert "provider error" in error
    assert cost_cents == 75
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 75
    assert hooks["settle_calls"][0]["report_usage"] is False
    assert hooks["usage_calls"] == []
    assert fake_store.create_synthesis_calls == []


# ===========================================================================
# thin_data (v2.1): DERIVED from the deterministic rollup batch, never from
# len(evidence). Two independent ways to end up thin: a low basis.
# total_publishable_quotes, or every rollup row coming back data_density='thin'.
# ===========================================================================

def test_run_synthesize_thin_rollup_basis_sets_thin_data_true(monkeypatch):
    """thin_data is TRUE when the rollup batch's own basis.total_publishable_quotes is
    below `_THIN_DATA_MIN_EVIDENCE` -- regardless of how many rows this run's separately
    capped citable-`evidence` list happens to carry."""
    evidence = _publishable_set(10)  # ample citable evidence -- deliberately NOT thin.
    fake_store = _FakeSynthesizeStore(
        evidence=evidence,
        rollup_header={
            "batch_id": "rollup-batch-1",
            "basis": {"total_publishable_quotes": 3, "as_of": "2026-01-01T00:00:00Z"},
            "computed_at": "2026-01-01T00:00:00Z", "store_id": "mv", "project_id": None,
        },
        rollup_rows=[
            {"theme": "fit", "quote_type": "complaint", "count": 3, "data_density": "normal",
             "member_quote_ids": ["quote-0"], "example_quote_ids": ["quote-0"]},
        ],
    )
    resp = _FakeResponse(_pain_payload([_pain(refs=[{"kind": "voc", "id": "quote-0"}])]))
    _wire(monkeypatch, fake_store, resp)

    status, _cost, _error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "done"
    call = fake_store.create_synthesis_calls[0]
    assert call["thin_data"] is True
    assert call["payload"]["rollup"]["total_publishable_quotes"] == 3


def test_run_synthesize_all_rollup_rows_thin_sets_thin_data_true(monkeypatch):
    """thin_data is ALSO true when every rollup row came back data_density='thin', even
    if the batch's own total_publishable_quotes count is at/above the threshold."""
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(
        evidence=evidence,
        rollup_header={
            "batch_id": "rollup-batch-1",
            "basis": {"total_publishable_quotes": 20, "as_of": "2026-01-01T00:00:00Z"},
            "computed_at": "2026-01-01T00:00:00Z", "store_id": "mv", "project_id": None,
        },
        rollup_rows=[
            {"theme": "fit", "quote_type": "complaint", "count": 2, "data_density": "thin",
             "member_quote_ids": ["quote-0"], "example_quote_ids": ["quote-0"]},
            {"theme": "shipping", "quote_type": "praise", "count": 1, "data_density": "thin",
             "member_quote_ids": ["quote-1"], "example_quote_ids": ["quote-1"]},
        ],
    )
    resp = _FakeResponse(_pain_payload([_pain(refs=[{"kind": "voc", "id": "quote-0"}])]))
    _wire(monkeypatch, fake_store, resp)

    status, _cost, _error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "done"
    assert fake_store.create_synthesis_calls[0]["thin_data"] is True


def test_run_synthesize_ample_rollup_sets_thin_data_false(monkeypatch):
    """The default fake-store rollup fixture (total_publishable_quotes=20, one 'normal'
    row) is deliberately ample -- confirms the mainline (non-thin) path."""
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    resp = _FakeResponse(_pain_payload([_pain(refs=[{"kind": "voc", "id": "quote-0"}])]))
    _wire(monkeypatch, fake_store, resp)

    status, _cost, _error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "done"
    assert fake_store.create_synthesis_calls[0]["thin_data"] is False


def test_run_synthesize_thin_data_ignores_zero_row_batch_vacuous_truth(monkeypatch):
    """A batch with zero rollup rows is NOT "all thin" by vacuous truth -- thin_data
    falls through to the count-based check alone. Ample total_publishable_quotes + zero
    rows -> NOT thin."""
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(
        evidence=evidence,
        rollup_header={
            "batch_id": "rollup-batch-1",
            "basis": {"total_publishable_quotes": 20, "as_of": "2026-01-01T00:00:00Z"},
            "computed_at": "2026-01-01T00:00:00Z", "store_id": "mv", "project_id": None,
        },
        rollup_rows=[],
    )
    resp = _FakeResponse(_pain_payload([_pain(refs=[{"kind": "voc", "id": "quote-0"}])]))
    _wire(monkeypatch, fake_store, resp)

    status, _cost, _error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "done"
    call = fake_store.create_synthesis_calls[0]
    assert call["thin_data"] is False
    assert call["payload"]["rollup"]["themes"] == []


# ===========================================================================
# Rollup wiring: compute called with (store_id, project_id), payload carries the
# rollup block pinned to batch_id, prompt is grounded in the rollup's counts.
# ===========================================================================

def test_run_synthesize_computes_rollup_before_reserve_with_store_and_project_id(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence, rollup_batch_id="batch-xyz")
    resp = _FakeResponse(_pain_payload([_pain(refs=[{"kind": "voc", "id": "quote-0"}])]))
    _wire(monkeypatch, fake_store, resp)

    synthesize.run_synthesize(_job(), "claimant-1")

    assert fake_store.compute_theme_rollups_calls == [("mv", "proj-1")]
    assert fake_store.get_theme_rollup_batch_calls == ["batch-xyz"]


def test_run_synthesize_payload_carries_rollup_block_pinned_to_batch_id(monkeypatch):
    evidence = _publishable_set(10)
    rollup_rows = [
        {"theme": "fit", "quote_type": "complaint", "count": 5, "data_density": "normal",
         "member_quote_ids": ["quote-0", "quote-1"], "example_quote_ids": ["quote-0"]},
        {"theme": "shipping", "quote_type": "praise", "count": 2, "data_density": "thin",
         "member_quote_ids": ["quote-2"], "example_quote_ids": ["quote-2"]},
    ]
    fake_store = _FakeSynthesizeStore(
        evidence=evidence, rollup_batch_id="batch-pinned",
        rollup_header={
            "id": "batch-pinned",
            "basis": {"total_publishable_quotes": 20, "as_of": "2026-01-05T12:00:00Z"},
            "computed_at": "2026-01-05T12:00:00Z", "store_id": "mv", "project_id": "proj-1",
        },
        rollup_rows=rollup_rows,
    )
    resp = _FakeResponse(_pain_payload([_pain(refs=[{"kind": "voc", "id": "quote-0"}])]))
    _wire(monkeypatch, fake_store, resp)

    status, _cost, _error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "done"
    call = fake_store.create_synthesis_calls[0]
    rollup_block = call["payload"]["rollup"]
    assert rollup_block["batch_id"] == "batch-pinned"
    assert rollup_block["as_of"] == "2026-01-05T12:00:00Z"
    assert rollup_block["total_publishable_quotes"] == 20
    assert rollup_block["themes"] == [
        {"theme": "fit", "quote_type": "complaint", "count": 5, "data_density": "normal"},
        {"theme": "shipping", "quote_type": "praise", "count": 2, "data_density": "thin"},
    ]
    assert call["schema_version"] == synthesize._SCHEMA_VERSION == 2


def test_run_synthesize_prompt_includes_rollup_grounding_table(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(
        evidence=evidence,
        rollup_rows=[
            {"theme": "fit", "quote_type": "complaint", "count": 5, "data_density": "normal",
             "member_quote_ids": ["quote-0"], "example_quote_ids": ["quote-0"]},
        ],
    )
    resp = _FakeResponse(_pain_payload([_pain(refs=[{"kind": "voc", "id": "quote-0"}])]))
    hooks = _wire(monkeypatch, fake_store, resp)

    synthesize.run_synthesize(_job(), "claimant-1")

    assert len(hooks["anthropic_calls"]) == 1
    sent_content = hooks["anthropic_calls"][0]["messages"][0]["content"]
    assert "DETERMINISTIC THEME ROLLUP" in sent_content
    assert "'fit'" in sent_content
    assert "count=5" in sent_content
    # The evidence block is still there too -- both data blocks reach the model.
    assert "PUBLISHABLE EVIDENCE" in sent_content


# ===========================================================================
# Rollup compute/read failure -- pre-reserve failure, same class as missing params /
# empty evidence: returns ("failed", 0, ...), nothing reserved, no paid call.
# ===========================================================================

def test_run_synthesize_rollup_compute_failure_fails_no_reserve(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(
        evidence=evidence, compute_rollup_raises=RuntimeError("rpc: bad store_id"),
    )
    hooks = _wire(monkeypatch, fake_store, resp=None)

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "failed"
    assert cost_cents == 0
    assert "theme-rollup compute failed" in error
    assert "bad store_id" in error
    assert hooks["reserve_calls"] == []
    assert hooks["anthropic_calls"] == []
    assert fake_store.get_theme_rollup_batch_calls == []
    assert fake_store.create_synthesis_calls == []


def test_run_synthesize_rollup_read_failure_fails_no_reserve(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(
        evidence=evidence, get_rollup_raises=ValueError("no batch header found"),
    )
    hooks = _wire(monkeypatch, fake_store, resp=None)

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "failed"
    assert cost_cents == 0
    assert "theme-rollup compute failed" in error
    assert "no batch header found" in error
    assert hooks["reserve_calls"] == []
    assert hooks["anthropic_calls"] == []
    assert fake_store.create_synthesis_calls == []


# ===========================================================================
# Lease fencing.
# ===========================================================================

def test_run_synthesize_lease_lost_before_reserving_spend_raises(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    hooks = _wire(monkeypatch, fake_store, resp=None, lease_values=False)

    with pytest.raises(RuntimeError, match="lease"):
        synthesize.run_synthesize(_job(), "claimant-1")

    assert hooks["reserve_calls"] == []
    assert fake_store.create_synthesis_calls == []


def test_run_synthesize_lease_lost_before_paid_call_settles_zero_then_raises(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    # True for the pre-reserve fence, False for the pre-call fence.
    hooks = _wire(monkeypatch, fake_store, resp=None, lease_values=[True, False])

    with pytest.raises(RuntimeError, match="lease"):
        synthesize.run_synthesize(_job(), "claimant-1")

    assert hooks["anthropic_calls"] == []
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 0
    assert fake_store.create_synthesis_calls == []


def test_run_synthesize_pre_call_lease_check_raising_settles_zero_then_raises(monkeypatch):
    """The second (pre-call) `jobs.assert_lease` RAISING (a DB error, not a lease-loss
    False return) must settle the reservation at actual_cents=0 before propagating --
    the exact orphaned-reservation class (Sol worker-gate P1) this fix closes. No API
    call has happened yet, so zero (not the worst case) is the correct settle amount."""
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    hooks = _wire(monkeypatch, fake_store, resp=None)

    calls = {"n": 0}

    def _flaky_assert_lease(job_id, claimant):
        calls["n"] += 1
        if calls["n"] == 1:
            return True  # pre-reserve fence: healthy.
        raise RuntimeError("db connection reset")  # pre-call fence: raises, not False.

    monkeypatch.setattr(synthesize.jobs, "assert_lease", _flaky_assert_lease)

    with pytest.raises(RuntimeError, match="db connection reset"):
        synthesize.run_synthesize(_job(), "claimant-1")

    assert hooks["anthropic_calls"] == []
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 0
    assert fake_store.create_synthesis_calls == []


def test_run_synthesize_anthropic_client_construction_failure_settles_zero_then_raises(monkeypatch):
    """`Anthropic(...)` raising (e.g. a missing/invalid API key) after the reservation
    but before any paid call must settle zero and propagate -- no tokens were ever
    billed, so orphaning the reservation here would be the same P1 class as the
    pre-call lease-raise case above."""
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    hooks = _wire(monkeypatch, fake_store, resp=None)

    def _raising_anthropic(api_key):
        raise ValueError("missing ANTHROPIC_API_KEY")

    monkeypatch.setattr(synthesize, "Anthropic", _raising_anthropic)

    with pytest.raises(ValueError, match="missing ANTHROPIC_API_KEY"):
        synthesize.run_synthesize(_job(), "claimant-1")

    assert hooks["anthropic_calls"] == []
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 0
    assert fake_store.create_synthesis_calls == []


def test_run_synthesize_schema_build_failure_settles_zero_then_raises(monkeypatch):
    """A failure building the structured-output schema (e.g. `_build_synthesis_prompt`
    tripping on a malformed evidence summary) after reserve, before the paid call, must
    also settle zero and propagate -- same guarded block, same P1 class."""
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    hooks = _wire(monkeypatch, fake_store, resp=None)

    def _raising_build_prompt(evidence):
        raise TypeError("malformed evidence summary")

    monkeypatch.setattr(synthesize, "_build_synthesis_prompt", _raising_build_prompt)

    with pytest.raises(TypeError, match="malformed evidence summary"):
        synthesize.run_synthesize(_job(), "claimant-1")

    assert hooks["anthropic_calls"] == []
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 0
    assert fake_store.create_synthesis_calls == []


def test_run_synthesize_rollup_block_build_failure_settles_zero_then_raises(monkeypatch):
    """Same guarded block, same P1 class as the schema/prompt-build failure above, but
    for `_build_rollup_block` -- the reservation has already been made by this point (the
    rollup READ itself succeeded back in step 3, pre-reserve), so a failure building its
    PROMPT rendering here must still settle zero, not orphan the reservation."""
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    hooks = _wire(monkeypatch, fake_store, resp=None)

    def _raising_build_rollup_block(rollup_rows):
        raise TypeError("malformed rollup row")

    monkeypatch.setattr(synthesize, "_build_rollup_block", _raising_build_rollup_block)

    with pytest.raises(TypeError, match="malformed rollup row"):
        synthesize.run_synthesize(_job(), "claimant-1")

    assert hooks["anthropic_calls"] == []
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 0
    assert fake_store.create_synthesis_calls == []


def test_run_synthesize_lease_lost_before_minting_raises_after_settle(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    resp = _FakeResponse(_pain_payload([_pain(refs=[{"kind": "voc", "id": "quote-0"}])]))
    # True, True for the two pre-call fences; False for the pre-mint fence.
    hooks = _wire(monkeypatch, fake_store, resp, lease_values=[True, True, False])

    with pytest.raises(RuntimeError, match="lease"):
        synthesize.run_synthesize(_job(), "claimant-1")

    # The paid call already happened and was already settled at the real cost --
    # losing the lease AFTER that must not re-settle or double-count.
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == synthesize._actual_cents(200, 80)
    assert fake_store.create_synthesis_calls == []


# ===========================================================================
# Budget: reserve worst-case then settle actual; usage_reporter.spend gets real token
# counts; settle report_usage=False (same discipline as run_collect/run_verify).
# ===========================================================================

def test_run_synthesize_reserve_ref_and_worst_case_then_settle_actual(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    resp = _FakeResponse(
        _pain_payload([_pain(refs=[{"kind": "voc", "id": "quote-0"}])]), input_tokens=500, output_tokens=40,
    )
    hooks = _wire(monkeypatch, fake_store, resp)

    synthesize.run_synthesize(_job(), "claimant-1")

    assert len(hooks["reserve_calls"]) == 1
    job_arg, ref_arg, claimant_arg, connector_arg = hooks["reserve_calls"][0]
    assert claimant_arg == "claimant-1"
    assert connector_arg is None
    assert ref_arg == "synthesize:job-1:claimant-1"

    settle = hooks["settle_calls"][0]
    assert settle["report_usage"] is False
    assert settle["reserved_est"] == 75
    assert settle["actual_cents"] == synthesize._actual_cents(500, 40)

    assert hooks["usage_calls"][0]["input_tokens"] == 500
    assert hooks["usage_calls"][0]["output_tokens"] == 40
    assert hooks["usage_calls"][0]["model"] == synthesize.settings.model


def test_run_synthesize_price_card_under_reserves_settles_zero_and_fails(monkeypatch):
    evidence = _publishable_set(10)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    hooks = _wire(monkeypatch, fake_store, resp=_FakeResponse(_pain_payload([])))
    monkeypatch.setenv("RESEARCH_MODEL", "some-unpriced-model")

    status, cost_cents, error = synthesize.run_synthesize(_job(), "claimant-1")

    assert status == "failed"
    assert cost_cents == 0
    assert "under-reserves" in error
    assert hooks["anthropic_calls"] == []
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 0


def test_run_synthesize_evidence_kind_scoping_uses_store_id_and_limit(monkeypatch):
    evidence = _publishable_set(5)
    fake_store = _FakeSynthesizeStore(evidence=evidence)
    resp = _FakeResponse(_pain_payload([_pain(refs=[{"kind": "voc", "id": "quote-0"}])]))
    _wire(monkeypatch, fake_store, resp)

    synthesize.run_synthesize(_job(), "claimant-1")

    assert fake_store.get_publishable_evidence_calls == [("mv", synthesize._MAX_EVIDENCE_ITEMS)]


# ===========================================================================
# Pure helpers.
# ===========================================================================

def test_build_synthesis_schema_shape():
    from worker import schema as schema_module
    built = schema_module.build_synthesis_schema()
    assert built["additionalProperties"] is False
    assert set(built["required"]) == {"title", "pains"}
    pain_items = built["properties"]["pains"]["items"]
    assert pain_items["additionalProperties"] is False
    assert set(pain_items["required"]) == {"theme", "summary", "evidence_refs"}
    ref_items = pain_items["properties"]["evidence_refs"]["items"]
    assert ref_items["additionalProperties"] is False
    assert set(ref_items["required"]) == {"kind", "id"}
    assert ref_items["properties"]["kind"]["enum"] == ["voc", "finding"]


def test_is_well_formed_synthesis_payload_accepts_valid_shape():
    assert synthesize._is_well_formed_synthesis_payload(_pain_payload([
        _pain(refs=[{"kind": "voc", "id": "q1"}]),
    ])) is True


def test_is_well_formed_synthesis_payload_accepts_empty_pains_list():
    assert synthesize._is_well_formed_synthesis_payload(_pain_payload([])) is True


@pytest.mark.parametrize("payload", [
    None,
    [1, 2, 3],
    "just a string",
    {},
    {"title": "t"},  # missing pains
    {"pains": []},  # missing title
    {"title": "", "pains": []},  # blank title
    {"title": "   ", "pains": []},  # whitespace-only title
    {"title": 123, "pains": []},  # non-string title
    {"title": "t", "pains": "not a list"},
    {"title": "t", "pains": [], "extra": "field"},  # unexpected extra key
])
def test_is_well_formed_synthesis_payload_rejects_malformed_shapes(payload):
    assert synthesize._is_well_formed_synthesis_payload(payload) is False


@pytest.mark.parametrize("pain", [
    None,
    "a string",
    {"theme": "t", "summary": "s"},  # missing evidence_refs
    {"theme": "", "summary": "s", "evidence_refs": []},  # blank theme
    {"theme": "t", "summary": "", "evidence_refs": []},  # blank summary
    {"theme": "t", "summary": "s", "evidence_refs": "not-a-list"},
    {"theme": "t", "summary": "s", "evidence_refs": [], "extra": 1},
    {"theme": "t", "summary": "s", "evidence_refs": [{"kind": "voc"}]},  # ref missing id
    {"theme": "t", "summary": "s", "evidence_refs": [{"kind": "bogus", "id": "x"}]},  # bad kind
])
def test_is_well_formed_pain_rejects_malformed_shapes(pain):
    assert synthesize._is_well_formed_pain(pain) is False


def test_is_well_formed_evidence_ref_rejects_extra_key():
    assert synthesize._is_well_formed_evidence_ref(
        {"kind": "voc", "id": "x", "confidence": "high"}
    ) is False


def test_derive_confidence_high_requires_evidence_and_citation_bar():
    assert synthesize._derive_confidence(total_evidence=10, cited_evidence=5) == "high"
    assert synthesize._derive_confidence(total_evidence=10, cited_evidence=4) != "high"
    assert synthesize._derive_confidence(total_evidence=7, cited_evidence=5) != "high"


def test_derive_confidence_medium_and_low():
    assert synthesize._derive_confidence(total_evidence=3, cited_evidence=2) == "medium"
    assert synthesize._derive_confidence(total_evidence=1, cited_evidence=1) == "low"
    assert synthesize._derive_confidence(total_evidence=0, cited_evidence=0) == "low"


def test_worst_case_cents_covers_a_realistic_high_token_input():
    """Mirrors `worker.verify`'s own worst-case regression test: the reservation must
    stay a genuine ceiling even for input that tokenizes at a HIGH rate -- INCLUDING the
    v2.1 rollup-grounding block -- and must stay within the deployed 'synthesize' price
    card's default ceiling (75 cents)."""
    total_capped_chars = (
        synthesize._SYSTEM_PROMPT_CHARS + synthesize._MAX_FRAMING_CHARS
        + synthesize._MAX_EVIDENCE_BLOCK_CHARS
        + synthesize._MAX_ROLLUP_FRAMING_CHARS + synthesize._MAX_ROLLUP_BLOCK_CHARS
        + synthesize._SCHEMA_CHARS + synthesize._PROTOCOL_OVERHEAD_CHARS
    )
    realistic_high_cents = synthesize._actual_cents(total_capped_chars, synthesize._MAX_TOKENS)
    worst_case = synthesize._worst_case_cents("claude-sonnet-5")

    assert worst_case >= realistic_high_cents
    assert worst_case <= 75


def test_build_synthesis_prompt_truncates_summary_to_max_chars():
    huge_summary = "x" * (synthesize._MAX_SUMMARY_CHARS + 1000)
    prompt = synthesize._build_synthesis_prompt([_evidence_row("q1", summary=huge_summary)])
    assert ("x" * synthesize._MAX_SUMMARY_CHARS) in prompt
    assert ("x" * (synthesize._MAX_SUMMARY_CHARS + 1)) not in prompt


def test_build_rollup_block_renders_rows():
    rows = [
        {"theme": "fit", "quote_type": "complaint", "count": 5, "data_density": "normal"},
        {"theme": "shipping", "quote_type": "praise", "count": 2, "data_density": "thin"},
    ]
    block = synthesize._build_rollup_block(rows)
    assert "DETERMINISTIC THEME ROLLUP" in block
    assert "'fit'" in block and "'complaint'" in block
    assert "count=5" in block and "(normal)" in block
    assert "'shipping'" in block and "'praise'" in block
    assert "count=2" in block and "(thin)" in block


def test_build_rollup_block_handles_empty_rows():
    block = synthesize._build_rollup_block([])
    assert "DETERMINISTIC THEME ROLLUP" in block
    assert "0 row(s)" in block


def test_build_rollup_block_truncates_field_length():
    huge_theme = "x" * (synthesize._MAX_ROLLUP_FIELD_CHARS + 50)
    block = synthesize._build_rollup_block(
        [{"theme": huge_theme, "quote_type": "t", "count": 1, "data_density": "thin"}]
    )
    assert ("x" * synthesize._MAX_ROLLUP_FIELD_CHARS) in block
    assert ("x" * (synthesize._MAX_ROLLUP_FIELD_CHARS + 1)) not in block


def test_build_rollup_block_caps_row_count():
    rows = [
        {"theme": f"theme-{i}", "quote_type": "t", "count": 1, "data_density": "thin"}
        for i in range(synthesize._MAX_ROLLUP_ROWS_IN_PROMPT + 10)
    ]
    block = synthesize._build_rollup_block(rows)
    assert block.count("- theme=") == synthesize._MAX_ROLLUP_ROWS_IN_PROMPT
