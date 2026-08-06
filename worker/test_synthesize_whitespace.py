"""Tests for the A3 WHITESPACE deliverable (Research Studio v2.1, migrations 174/176):
worker/synthesize.py's `run_whitespace_synthesis` + its pure helpers, and the worker/store.py
seam it depends on (`compute_angle_rollups`, `get_angle_rollup_batch`).

No network, no real Supabase, no real Anthropic call: every collaborator (`synthesize.store`,
`synthesize.schema.active_angle_registry`, `synthesize.jobs.assert_lease`,
`synthesize.budget.reserve`/`settle`, `synthesize.Anthropic`, `synthesize.usage_reporter`) is
monkeypatched directly on the module objects that use them -- the exact same pattern
`worker/test_synthesize.py` uses for the VoC pain-map path. The deployed RPCs themselves
(`rs_compute_angle_rollups`/174, `rs_create_synthesis`/`rs_synthesis_all_refs_publishable`/
`rs_publish_synthesis` rollup legs/176) are NOT re-tested here -- those migrations own that; this
file tests only the Python driving them.
"""
import json

import pytest

from worker import schema, store, synthesize
from worker.budget import ReserveResult


# ===========================================================================
# worker/store.py -- compute_angle_rollups / get_angle_rollup_batch (v2.1, migration 174/176
# angle-rollup wiring), tested directly against fake Supabase clients.
# ===========================================================================

class _FakeResult:
    def __init__(self, data):
        self.data = data


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


def test_compute_angle_rollups_sends_no_params_and_returns_batch_id(monkeypatch):
    client = _FakeRpcClient(rpc_response="angle-batch-1")
    monkeypatch.setattr(store, "_client", lambda: client)

    batch_id = store.compute_angle_rollups()

    assert batch_id == "angle-batch-1"
    assert client.rpcs[0].name == "rs_compute_angle_rollups"
    # 174: the RPC takes NO parameters at all (global rollup, no store/project scope).
    assert client.rpcs[0].params == {}


def test_compute_angle_rollups_propagates_rpc_exception(monkeypatch):
    client = _FakeRpcClient(raise_exc=RuntimeError("invariant violated"))
    monkeypatch.setattr(store, "_client", lambda: client)
    with pytest.raises(RuntimeError, match="invariant violated"):
        store.compute_angle_rollups()


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


class _FakeMultiTableClient:
    """Routes `.table(name)` to a per-table fixed row list -- `get_angle_rollup_batch` reads from
    TWO distinct tables (`research_angle_rollup_batches` header, `research_angle_rollups` rows) in
    one call, so the fake must tell them apart."""

    def __init__(self, tables: dict):
        self._tables = tables

    def table(self, name):
        return _FakeQuery(self._tables.get(name, []))


def test_get_angle_rollup_batch_returns_header_and_rows_with_id(monkeypatch):
    header_row = {
        "batch_id": "angle-batch-1",
        "basis": {"selected_competitors": 8, "competitors_with_observations": 6, "coverage_gap": 2},
        "computed_at": "2026-08-06T00:00:00Z", "schema_version": 1,
    }
    matching_row = {
        "id": "row-1", "angle_key": "fit_curvy", "competitor_count": 3, "total_ad_count": 6,
        "body_verbatim_count": 2, "not_body_verified_count": 1,
        "member_competitor_ids": ["c1", "c2", "c3"], "member_analysis_ids": ["a1", "a2", "a3"],
        "batch_id": "angle-batch-1",
    }
    other_batch_row = {
        "id": "row-OTHER", "angle_key": "nightwear", "competitor_count": 1, "total_ad_count": 1,
        "body_verbatim_count": 1, "not_body_verified_count": 0,
        "member_competitor_ids": ["c9"], "member_analysis_ids": ["a9"], "batch_id": "angle-batch-OTHER",
    }
    client = _FakeMultiTableClient({
        "research_angle_rollup_batches": [header_row],
        "research_angle_rollups": [matching_row, other_batch_row],
    })
    monkeypatch.setattr(store, "_client", lambda: client)

    result = store.get_angle_rollup_batch("angle-batch-1")

    assert result == {"header": header_row, "rows": [matching_row]}
    # The row's own `id` MUST be selected -- unlike the theme-rollup seam, an angle-rollup row is
    # itself citable evidence (176), so `run_whitespace_synthesis` needs it to build a ref.
    assert "id" in result["rows"][0]


def test_get_angle_rollup_batch_raises_when_header_missing(monkeypatch):
    client = _FakeMultiTableClient({"research_angle_rollup_batches": [], "research_angle_rollups": []})
    monkeypatch.setattr(store, "_client", lambda: client)

    with pytest.raises(ValueError, match="batch_id"):
        store.get_angle_rollup_batch("missing-batch")


def test_get_angle_rollup_batch_returns_empty_rows_when_batch_has_none(monkeypatch):
    """A genuinely EMPTY batch (0 rollup rows) is a real, legitimate outcome -- distinct from a
    missing header -- so it must NOT raise; `run_whitespace_synthesis`'s degenerate-census refusal
    is the layer that decides what to do with it."""
    header_row = {
        "batch_id": "angle-batch-empty",
        "basis": {"selected_competitors": 3, "competitors_with_observations": 0, "coverage_gap": 3},
        "computed_at": "2026-08-06T00:00:00Z", "schema_version": 1,
    }
    client = _FakeMultiTableClient({
        "research_angle_rollup_batches": [header_row], "research_angle_rollups": [],
    })
    monkeypatch.setattr(store, "_client", lambda: client)

    result = store.get_angle_rollup_batch("angle-batch-empty")

    assert result == {"header": header_row, "rows": []}


# ===========================================================================
# worker/synthesize.py -- run_whitespace_synthesis, stubbed Anthropic + store + registry.
# ===========================================================================

class _FakeUsage:
    def __init__(self, input_tokens=150, output_tokens=60):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, payload, input_tokens=150, output_tokens=60):
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
    def __init__(self, exc):
        self._exc = exc

    @property
    def messages(self):
        return self

    def stream(self, **kwargs):
        raise self._exc


def _default_registry():
    return [
        {"angle_key": "fit_curvy", "label": "Fit / curvy", "definition": "Fit inclusivity."},
        {"angle_key": "morning_ritual", "label": "Morning ritual", "definition": "MV whitespace candidate."},
        {"angle_key": "unmapped", "label": "Unmapped", "definition": "No registry angle fits."},
    ]


def _default_basis(**overrides):
    basis = {
        "scope": "global_competitor_set", "classification": "model_asserted",
        "countable_basis": "provenance_verified", "countable_set": "latest_ok_analysis_per_competitor",
        "recency_order": "created_at DESC, id DESC", "unmapped": "present_not_a_real_angle",
        "grade_is_display_only": True,
        "selected_competitors": 8, "competitors_with_observations": 6, "coverage_gap": 0,
    }
    basis.update(overrides)
    return basis


def _default_header(batch_id="ws-batch-1", **basis_overrides):
    return {
        "batch_id": batch_id, "basis": _default_basis(**basis_overrides),
        "computed_at": "2026-08-06T00:00:00Z", "schema_version": 1,
    }


def _angle_row(angle_key, row_id=None, competitor_count=2, total_ad_count=4,
               body_verbatim_count=1, not_body_verified_count=1):
    # A rollup row only EXISTS for an angle with >=1 observation (174 groups by angle_key over
    # real observations) -- competitor_count=0 is not a legal row shape; a zero-coverage angle
    # has NO row at all (see the registry fixture's "morning_ritual", which never appears here).
    assert competitor_count >= 1
    return {
        "id": row_id or f"row-{angle_key}",
        "angle_key": angle_key, "competitor_count": competitor_count, "total_ad_count": total_ad_count,
        "body_verbatim_count": body_verbatim_count, "not_body_verified_count": not_body_verified_count,
        "member_competitor_ids": [f"c{i}" for i in range(competitor_count)],
        "member_analysis_ids": [f"a{i}" for i in range(competitor_count)],
    }


def _default_rows():
    # "fit_curvy" HAS coverage; "morning_ritual" (in the registry) has NO row -- the genuine
    # whitespace-via-absence case.
    return [_angle_row("fit_curvy", competitor_count=3, total_ad_count=6, body_verbatim_count=2, not_body_verified_count=1)]


class _FakeWhitespaceStore:
    def __init__(
        self, rollup_batch_id="ws-batch-1", rollup_header=None, rollup_rows=None,
        compute_raises=None, get_raises=None, create_raises=None, create_returns="syn-ws-1",
    ):
        self.rollup_batch_id = rollup_batch_id
        self.rollup_header = rollup_header if rollup_header is not None else _default_header(rollup_batch_id)
        self.rollup_rows = rollup_rows if rollup_rows is not None else _default_rows()
        self.compute_raises = compute_raises
        self.get_raises = get_raises
        self.create_raises = create_raises
        self.create_returns = create_returns
        self.compute_angle_rollups_calls = 0
        self.get_angle_rollup_batch_calls = []
        self.create_synthesis_calls = []

    def compute_angle_rollups(self):
        self.compute_angle_rollups_calls += 1
        if self.compute_raises is not None:
            raise self.compute_raises
        return self.rollup_batch_id

    def get_angle_rollup_batch(self, batch_id):
        self.get_angle_rollup_batch_calls.append(batch_id)
        if self.get_raises is not None:
            raise self.get_raises
        return {"header": self.rollup_header, "rows": list(self.rollup_rows)}

    def rs_create_synthesis(self, **kwargs):
        self.create_synthesis_calls.append(kwargs)
        if self.create_raises is not None:
            raise self.create_raises
        return self.create_returns


def _ws_payload(candidates):
    return {"candidates": candidates}


def _candidate(angle_key="morning_ritual", summary="No captured competitors run a morning-ritual framing."):
    return {"angle_key": angle_key, "summary": summary}


def _wire_ws(
    monkeypatch, fake_store, resp=None, lease_values=True, reserve_result=None,
    anthropic_client=None, registry=None,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(synthesize, "store", fake_store)
    monkeypatch.setattr(schema, "active_angle_registry", lambda: (registry if registry is not None else _default_registry()))

    if isinstance(lease_values, list):
        it = iter(lease_values)
        monkeypatch.setattr(synthesize.jobs, "assert_lease", lambda job_id, claimant: next(it))
    else:
        monkeypatch.setattr(synthesize.jobs, "assert_lease", lambda job_id, claimant: lease_values)

    if reserve_result is None:
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


def _job_ws(**overrides):
    job = {
        "id": "job-ws-1", "job_kind": "synthesize", "project_id": "proj-1",
        "params": {"store_id": "mv", "kind": "whitespace", "project_id": "proj-1"},
    }
    job.update(overrides)
    return job


# ===========================================================================
# Dispatch: run_synthesize routes kind='whitespace' to run_whitespace_synthesis.
# ===========================================================================

def test_run_synthesize_dispatches_whitespace_kind_to_run_whitespace_synthesis(monkeypatch):
    fake_store = _FakeWhitespaceStore()
    resp = _FakeResponse(_ws_payload([_candidate()]))
    _wire_ws(monkeypatch, fake_store, resp)

    status, cost_cents, error = synthesize.run_synthesize(_job_ws(), "claimant-1")

    assert status == "done"
    assert error is None
    assert fake_store.compute_angle_rollups_calls == 1
    assert len(fake_store.create_synthesis_calls) == 1
    assert fake_store.create_synthesis_calls[0]["kind"] == "whitespace"


# ===========================================================================
# P1 (diff-gate): a model `summary` containing a numeral is a model-authored number with nothing
# to validate it against -- it is DROPPED by mechanism, never minted. The counts live in the
# worker-injected structured fields, so an honest qualitative summary never needs a digit.
# ===========================================================================

def test_summary_number_detector_digit_word_and_qualitative():
    f = synthesize._summary_has_model_authored_number
    assert f("Only 1 captured competitor runs this.") is True          # digit
    assert f("Only one captured competitor runs this.") is True         # spelled cardinal
    assert f("About a dozen competitors converge here.") is True        # 'dozen'
    assert f("A pair of captured competitors use this framing.") is True  # 'pair'
    assert f("No captured competitor runs this framing.") is False      # qualitative absence
    assert f("Few of the captured competitors emphasise this.") is False  # qualitative
    assert f("Most of the captured set converges here already.") is False  # qualitative


def test_run_whitespace_synthesis_drops_candidate_with_number_in_summary(monkeypatch):
    # A spelled-out cardinal ('one') is just as much a model-authored count as a digit -> dropped.
    fake_store = _FakeWhitespaceStore()
    resp = _FakeResponse(_ws_payload([
        _candidate(angle_key="fit_curvy", summary="Only one captured competitor runs this."),       # word count -> dropped
        _candidate(angle_key="morning_ritual", summary="No captured competitor runs this framing."),  # clean -> kept
    ]))
    _wire_ws(monkeypatch, fake_store, resp)

    status, _cents, error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "done", error
    minted = fake_store.create_synthesis_calls[0]["payload"]["candidates"]
    keys = {c["angle_key"] for c in minted}
    assert "fit_curvy" not in keys, "a summary stating a specific count (digit or word) must be dropped"
    assert "morning_ritual" in keys
    assert all(not synthesize._summary_has_model_authored_number(c["summary"]) for c in minted)


def test_run_whitespace_synthesis_excludes_unmapped_sentinel(monkeypatch):
    # 'unmapped' is the classifier escape-hatch, not a real angle -- it must be neither in the model
    # schema enum nor mintable as a whitespace candidate even if the model names it.
    fake_store = _FakeWhitespaceStore()
    resp = _FakeResponse(_ws_payload([
        _candidate(angle_key="unmapped", summary="No competitor fits a named angle here."),         # excluded -> dropped
        _candidate(angle_key="morning_ritual", summary="No captured competitor runs this framing."),  # kept
    ]))
    _wire_ws(monkeypatch, fake_store, resp)

    # The model-facing schema enum must not offer 'unmapped'.
    built = schema.build_whitespace_schema(
        [k for k in schema.angle_keys_from_registry(_default_registry()) if k != "unmapped"]
    )
    enum = built["properties"]["candidates"]["items"]["properties"]["angle_key"]["enum"]
    assert "unmapped" not in enum

    status, _cents, error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")
    assert status == "done", error
    minted = fake_store.create_synthesis_calls[0]["payload"]["candidates"]
    keys = {c["angle_key"] for c in minted}
    assert "unmapped" not in keys, "the 'unmapped' sentinel must never be minted as a whitespace candidate"
    assert "morning_ritual" in keys


# ===========================================================================
# store_id required -- fails before anything is touched.
# ===========================================================================

def test_run_whitespace_synthesis_missing_store_id_fails_without_reserve(monkeypatch):
    fake_store = _FakeWhitespaceStore()
    hooks = _wire_ws(monkeypatch, fake_store, resp=None)

    job = _job_ws(params={"kind": "whitespace"})
    status, cost_cents, error = synthesize.run_whitespace_synthesis(job, "claimant-1")

    assert status == "failed"
    assert cost_cents == 0
    assert "store_id" in error
    assert hooks["reserve_calls"] == []
    assert fake_store.compute_angle_rollups_calls == 0
    assert fake_store.create_synthesis_calls == []


# ===========================================================================
# Degenerate-census refusal -- zero rollup rows, refused BEFORE any reserve.
# ===========================================================================

def test_run_whitespace_synthesis_degenerate_census_refuses_before_reserve(monkeypatch):
    fake_store = _FakeWhitespaceStore(rollup_rows=[])
    hooks = _wire_ws(monkeypatch, fake_store, resp=None)

    status, cost_cents, error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "failed"
    assert cost_cents == 0
    assert "degenerate census" in error
    assert hooks["reserve_calls"] == []
    assert hooks["anthropic_calls"] == []
    assert fake_store.create_synthesis_calls == []


def test_run_whitespace_synthesis_rollup_compute_failure_fails_no_reserve(monkeypatch):
    fake_store = _FakeWhitespaceStore(compute_raises=RuntimeError("db exploded"))
    hooks = _wire_ws(monkeypatch, fake_store, resp=None)

    status, cost_cents, error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "failed"
    assert cost_cents == 0
    assert "angle-rollup compute failed" in error
    assert hooks["reserve_calls"] == []


# ===========================================================================
# thin_data / confidence -- deterministic, derived ONLY from the batch's own `basis`.
# ===========================================================================

def test_derive_whitespace_thin_data_true_when_coverage_gap_positive():
    basis = _default_basis(coverage_gap=1, competitors_with_observations=8)
    assert synthesize._derive_whitespace_thin_data(basis) is True


def test_derive_whitespace_thin_data_true_when_below_floor():
    basis = _default_basis(coverage_gap=0, competitors_with_observations=1)
    assert synthesize._derive_whitespace_thin_data(basis) is True


def test_derive_whitespace_thin_data_false_when_clean_and_above_floor():
    basis = _default_basis(coverage_gap=0, competitors_with_observations=6)
    assert synthesize._derive_whitespace_thin_data(basis) is False


def test_derive_whitespace_thin_data_true_on_malformed_basis():
    assert synthesize._derive_whitespace_thin_data({}) is True
    assert synthesize._derive_whitespace_thin_data(None) is True


def test_derive_whitespace_confidence_high_requires_clean_batch_and_floor():
    basis = _default_basis(coverage_gap=0, competitors_with_observations=6)
    assert synthesize._derive_whitespace_confidence(basis) == "high"


def test_derive_whitespace_confidence_not_high_with_any_coverage_gap():
    basis = _default_basis(coverage_gap=1, competitors_with_observations=10)
    assert synthesize._derive_whitespace_confidence(basis) == "medium"


def test_derive_whitespace_confidence_medium_and_low():
    assert synthesize._derive_whitespace_confidence(_default_basis(competitors_with_observations=3, coverage_gap=1)) == "medium"
    assert synthesize._derive_whitespace_confidence(_default_basis(competitors_with_observations=1, coverage_gap=0)) == "low"
    assert synthesize._derive_whitespace_confidence({}) == "low"


def test_run_whitespace_synthesis_thin_data_and_confidence_flow_into_mint(monkeypatch):
    fake_store = _FakeWhitespaceStore(
        rollup_header=_default_header(competitors_with_observations=1, coverage_gap=2),
    )
    resp = _FakeResponse(_ws_payload([_candidate()]))
    _wire_ws(monkeypatch, fake_store, resp)

    status, _cost, _error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "done"
    call = fake_store.create_synthesis_calls[0]
    assert call["thin_data"] is True
    assert call["confidence"] == "low"


# ===========================================================================
# Worker-built evidence_refs -- batch header + EVERY rollup row, regardless of what the model's
# candidates actually name. The model's own output is NEVER a source of refs or counts.
# ===========================================================================

def test_run_whitespace_synthesis_evidence_refs_are_batch_plus_all_rows(monkeypatch):
    rows = [
        _angle_row("fit_curvy", row_id="row-fit", competitor_count=3),
        _angle_row("nightwear", row_id="row-night", competitor_count=1),
    ]
    fake_store = _FakeWhitespaceStore(rollup_rows=rows)
    # The model only discusses ONE angle_key (not even one of the two WITH rows) -- refs must
    # still cover the batch header + BOTH rows, untouched by what the model said.
    resp = _FakeResponse(_ws_payload([_candidate(angle_key="morning_ritual")]))
    _wire_ws(monkeypatch, fake_store, resp)

    status, _cost, _error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "done"
    call = fake_store.create_synthesis_calls[0]
    refs = call["evidence_refs"]
    assert {"table": "research_angle_rollup_batches", "id": "ws-batch-1"} in refs
    assert {"table": "research_angle_rollups", "id": "row-fit"} in refs
    assert {"table": "research_angle_rollups", "id": "row-night"} in refs
    assert len(refs) == 3  # batch + 2 rows -- exactly, nothing more, nothing fewer.


def test_run_whitespace_synthesis_row_missing_id_fails_before_reserve(monkeypatch):
    bad_row = _angle_row("fit_curvy")
    del bad_row["id"]
    fake_store = _FakeWhitespaceStore(rollup_rows=[bad_row])
    hooks = _wire_ws(monkeypatch, fake_store, resp=None)

    status, cost_cents, error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "failed"
    assert cost_cents == 0
    assert "missing its own id" in error
    assert hooks["reserve_calls"] == []


# ===========================================================================
# Happy path -- worker-composed title, worker-assembled scope/basis payload, worker-injected
# per-candidate counts.
# ===========================================================================

def test_run_whitespace_synthesis_happy_path_mints_with_worker_built_payload(monkeypatch):
    fake_store = _FakeWhitespaceStore(create_returns="syn-ws-42")
    resp = _FakeResponse(_ws_payload([
        _candidate(angle_key="fit_curvy", summary="Some coverage, worth watching."),
        _candidate(angle_key="morning_ritual", summary="No captured competitors run this."),
    ]))
    hooks = _wire_ws(monkeypatch, fake_store, resp)

    status, cost_cents, error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "done"
    assert error is None
    assert cost_cents == synthesize._actual_cents(150, 60)

    assert len(hooks["reserve_calls"]) == 1
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == cost_cents
    assert hooks["settle_calls"][0]["report_usage"] is False

    call = fake_store.create_synthesis_calls[0]
    assert call["store_id"] == "mv"
    assert call["kind"] == "whitespace"
    assert call["schema_version"] == 1  # the registered research_synthesis_kinds row (migr 032).
    assert call["origin"] == "agent"
    assert call["area"] == "ad_angles"  # the stable default supersede-index area.
    assert call["project_id"] == "proj-1"

    # Title is WORKER-composed, carries the coverage bound verbatim.
    assert call["title"] == call["payload"]["title"]
    assert "among 6 captured competitors" in call["title"]
    assert "coverage-bounded" in call["title"]
    assert "not an MV gap analysis" in call["title"]

    # Scope block states the honesty framing plainly.
    assert call["payload"]["scope"] == {
        "prepared_for_store_id": "mv", "census": "global_tracked_competitor_set",
        "own_plays_captured": False, "classification": "model_asserted",
    }
    assert call["payload"]["basis"] == fake_store.rollup_header["basis"]
    assert call["payload"]["batch_id"] == "ws-batch-1"

    # Per-candidate counts are WORKER-INJECTED from the rollup read, never the model's own text.
    by_key = {c["angle_key"]: c for c in call["payload"]["candidates"]}
    assert by_key["fit_curvy"]["competitor_count"] == 3
    assert by_key["fit_curvy"]["body_verbatim_count"] == 2
    assert by_key["fit_curvy"]["has_rollup_row"] is True
    assert by_key["morning_ritual"]["competitor_count"] == 0
    assert by_key["morning_ritual"]["has_rollup_row"] is False

    assert len(hooks["usage_calls"]) == 1
    assert hooks["usage_calls"][0]["meta"]["rollup_batch_id"] == "ws-batch-1"


# ===========================================================================
# Registry-membership validation -- defense-in-depth beyond the schema enum; dedup.
# ===========================================================================

def test_run_whitespace_synthesis_drops_out_of_registry_angle_key(monkeypatch):
    fake_store = _FakeWhitespaceStore()
    resp = _FakeResponse(_ws_payload([
        _candidate(angle_key="fit_curvy"),
        {"angle_key": "not_a_real_registry_key", "summary": "hallucinated"},
    ]))
    _wire_ws(monkeypatch, fake_store, resp)

    status, _cost, _error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "done"
    call = fake_store.create_synthesis_calls[0]
    keys = [c["angle_key"] for c in call["payload"]["candidates"]]
    assert keys == ["fit_curvy"]
    assert "not_a_real_registry_key" not in json.dumps(call)


def test_run_whitespace_synthesis_dedups_duplicate_angle_key(monkeypatch):
    fake_store = _FakeWhitespaceStore()
    resp = _FakeResponse(_ws_payload([
        _candidate(angle_key="fit_curvy", summary="first"),
        _candidate(angle_key="fit_curvy", summary="duplicate, must be dropped"),
    ]))
    _wire_ws(monkeypatch, fake_store, resp)

    status, _cost, _error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "done"
    call = fake_store.create_synthesis_calls[0]
    assert len(call["payload"]["candidates"]) == 1


def test_run_whitespace_synthesis_zero_valid_candidates_fails_no_mint(monkeypatch):
    fake_store = _FakeWhitespaceStore()
    resp = _FakeResponse(_ws_payload([]))
    _wire_ws(monkeypatch, fake_store, resp)

    status, cost_cents, error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "failed"
    assert "no valid whitespace candidates" in error
    assert cost_cents == synthesize._actual_cents(150, 60)  # spend already settled.
    assert fake_store.create_synthesis_calls == []


# ===========================================================================
# Registry-size cost-bound guard -- pre-reserve, never silently under-reserves.
# ===========================================================================

def test_run_whitespace_synthesis_oversized_registry_fails_before_reserve(monkeypatch):
    fake_store = _FakeWhitespaceStore()
    oversized_registry = [
        {"angle_key": f"key_{i}", "label": f"Key {i}", "definition": ""}
        for i in range(synthesize._MAX_ANGLE_REGISTRY_SIZE + 1)
    ]
    hooks = _wire_ws(monkeypatch, fake_store, resp=None, registry=oversized_registry)

    status, cost_cents, error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "failed"
    assert cost_cents == 0
    assert "exceeding the cost-bound ceiling" in error
    assert hooks["reserve_calls"] == []


# ===========================================================================
# Reserve/settle/lease discipline -- mirrors run_synthesize's own budget-gated call exactly.
# ===========================================================================

def test_run_whitespace_synthesis_reserve_skip_replay_settles_at_ceiling(monkeypatch):
    fake_store = _FakeWhitespaceStore()
    skip_result = ReserveResult(ok=False, reserved_est_cents=75, project_scoped=True)
    hooks = _wire_ws(monkeypatch, fake_store, resp=None, reserve_result=skip_result)

    status, cost_cents, error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "failed"
    assert cost_cents == 75
    assert "reserve skip replay" in error
    assert hooks["anthropic_calls"] == []
    assert fake_store.create_synthesis_calls == []


def test_run_whitespace_synthesis_provider_error_settles_worst_case(monkeypatch):
    fake_store = _FakeWhitespaceStore()
    hooks = _wire_ws(
        monkeypatch, fake_store,
        anthropic_client=_RaisingAnthropicClient(RuntimeError("connection reset")),
    )

    status, cost_cents, error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "failed"
    assert "provider error" in error
    assert cost_cents == 75
    assert hooks["settle_calls"][0]["actual_cents"] == 75
    assert fake_store.create_synthesis_calls == []


def test_run_whitespace_synthesis_lease_lost_before_reserving_spend_raises(monkeypatch):
    fake_store = _FakeWhitespaceStore()
    hooks = _wire_ws(monkeypatch, fake_store, resp=None, lease_values=False)

    with pytest.raises(RuntimeError, match="lease lost"):
        synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert hooks["reserve_calls"] == []


def test_run_whitespace_synthesis_rs_create_synthesis_rejection_fails_with_settled_cost(monkeypatch):
    fake_store = _FakeWhitespaceStore(create_raises=RuntimeError("evidence ref no longer publishable"))
    resp = _FakeResponse(_ws_payload([_candidate()]))
    hooks = _wire_ws(monkeypatch, fake_store, resp)

    status, cost_cents, error = synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    assert status == "failed"
    assert "rs_create_synthesis rejected refs" in error
    assert cost_cents == synthesize._actual_cents(150, 60)
    assert hooks["settle_calls"][0]["actual_cents"] == cost_cents


def test_run_whitespace_synthesis_computes_rollup_before_reserve(monkeypatch):
    fake_store = _FakeWhitespaceStore()
    resp = _FakeResponse(_ws_payload([_candidate()]))
    hooks = _wire_ws(monkeypatch, fake_store, resp)

    synthesize.run_whitespace_synthesis(_job_ws(), "claimant-1")

    # The rollup compute+read (and the registry fetch) happen strictly before budget.reserve().
    assert fake_store.compute_angle_rollups_calls == 1
    assert fake_store.get_angle_rollup_batch_calls == ["ws-batch-1"]
    assert len(hooks["reserve_calls"]) == 1


# ===========================================================================
# Grade-aware, coverage-bounded grounding -- pure helper tests, no network.
# ===========================================================================

def test_system_whitespace_prompt_forbids_fabricated_quotes_and_absolute_claims():
    prompt = synthesize.SYSTEM_WHITESPACE
    assert "NOT given any competitor ad's actual hook" in prompt
    assert "fabricated" in prompt
    assert "no competitor runs this angle" in prompt  # named as the FORBIDDEN framing
    assert "among the N competitors we captured" in prompt  # the REQUIRED framing
    assert "model_asserted" in prompt.lower() or "MODEL-ASSERTED" in prompt


def test_build_angle_census_block_marks_absent_angle_as_no_rollup_row():
    registry = _default_registry()  # includes "morning_ritual", which has no row below.
    rows = _default_rows()  # only "fit_curvy" has a row.
    block = synthesize._build_angle_census_block(registry, rows, _default_basis())

    assert "angle_key='morning_ritual'" in block
    assert "NO ROLLUP ROW" in block
    assert "angle_key='fit_curvy'" in block
    assert "competitor_count=3" in block


def test_build_angle_census_block_never_renders_hook_text_only_counts():
    """Grade-aware framing: a `not_body_verified`-heavy row is rendered as a COUNT, never as a
    quoted hook -- the census block carries no ad-copy text field at all for the model to draw a
    'quote' from in the first place."""
    rows = [_angle_row("fit_curvy", competitor_count=2, body_verbatim_count=0, not_body_verified_count=2)]
    block = synthesize._build_angle_census_block(_default_registry(), rows, _default_basis())

    assert "not_body_verified=2" in block
    assert "body_verbatim=0" in block
    # No quotation marks around any ad-copy-shaped text -- only the deterministic count line.
    assert '"' not in block


def test_build_angle_census_block_caps_registry_size():
    oversized_registry = [
        {"angle_key": f"key_{i}", "label": f"Key {i}", "definition": ""}
        for i in range(synthesize._MAX_ANGLE_REGISTRY_SIZE + 10)
    ]
    block = synthesize._build_angle_census_block(oversized_registry, [], _default_basis())
    assert block.count("angle_key=") == synthesize._MAX_ANGLE_REGISTRY_SIZE


def test_build_angle_census_block_states_coverage_basis():
    block = synthesize._build_angle_census_block([], [], _default_basis(selected_competitors=8, competitors_with_observations=6, coverage_gap=0))
    assert "selected_competitors=8" in block
    assert "competitors_with_observations=6" in block
    assert "coverage_gap=0" in block


# ===========================================================================
# Shape validation -- worker/synthesize.py's whitespace-payload guards.
# ===========================================================================

def test_is_well_formed_whitespace_payload_accepts_valid_shape():
    assert synthesize._is_well_formed_whitespace_payload({"candidates": [_candidate()]}) is True


def test_is_well_formed_whitespace_payload_accepts_empty_candidates_list():
    assert synthesize._is_well_formed_whitespace_payload({"candidates": []}) is True


@pytest.mark.parametrize("payload", [
    None, [], "x", {}, {"candidates": "not-a-list"},
    {"candidates": [], "extra": 1},
    {"candidates": [{"angle_key": "x"}]},  # missing summary
    {"candidates": [{"angle_key": "", "summary": "y"}]},  # blank angle_key
])
def test_is_well_formed_whitespace_payload_rejects_malformed_shapes(payload):
    assert synthesize._is_well_formed_whitespace_payload(payload) is False


def test_build_whitespace_schema_shape():
    built = schema.build_whitespace_schema(["fit_curvy", "morning_ritual"])
    assert set(built["properties"]) == {"candidates"}
    assert built["required"] == ["candidates"]
    candidate_schema = built["properties"]["candidates"]["items"]
    assert candidate_schema["properties"]["angle_key"]["enum"] == ["fit_curvy", "morning_ritual"]


def test_worst_case_whitespace_cents_covers_realistic_input():
    # A sanity floor: the worst-case ceiling must be a positive number of cents for the
    # configured model, matching `test_worst_case_cents_covers_a_realistic_high_token_input`'s own
    # style in test_synthesize.py.
    cents = synthesize._worst_case_whitespace_cents("claude-sonnet-5")
    assert cents > 0
