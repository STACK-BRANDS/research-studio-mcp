"""Tests for the `verify` job (P4 VERIFICATION of the deep-research plan):
worker/verify.py's `run_verify` + its pure helpers.

No network, no real Supabase, no real Anthropic call: every collaborator (`verify.store`,
`verify.jobs.assert_lease`, `verify.budget.reserve`/`settle`, `verify.Anthropic`,
`verify.usage_reporter`) is monkeypatched directly on the module objects that use them --
the exact same pattern `worker/test_extract.py` uses for `run_collect`. The deployed
RPCs themselves (freeze/integrity/outcome/finalize) are NOT re-tested here -- migration
150 owns that; this file tests only the Python driving them.
"""
import json

import pytest
from storage3.exceptions import StorageApiError

from worker import verify
from worker.budget import ReserveResult
from worker.canonical import canonical_text

RAW_HTML = (
    "<html><body>"
    "<div class='hero'>Buy 2 get 1 free, today only!</div>"
    "<div class='jdgm-rev-widg'><div class='jdgm-rev__body'>"
    "incredibly comfortable and true to size"
    "</div></div>"
    "</body></html>"
)
CANONICAL_TEXT = canonical_text(RAW_HTML)
REVIEW_TEXT = verify.extract_review_text(RAW_HTML)


def _capture(**overrides):
    row = {
        "id": "cap-1", "competitor_id": "comp-1", "url": "https://competitor.com/p",
        "source_url": "https://competitor.com/p", "captured_html_path": "captures/rawsha.html",
        "content_sha256": "canonsha", "connector": "web.fetch",
    }
    row.update(overrides)
    return row


def _member(evidence_kind, evidence_id, result="pending", capture_id="cap-1", sample_rank=1):
    return {
        "job_id": "job-1", "evidence_kind": evidence_kind, "evidence_id": evidence_id,
        "capture_id": capture_id, "sample_rank": sample_rank, "result": result,
    }


def _voc_row(**overrides):
    row = {
        "id": "quote-1", "quote": "incredibly comfortable and true to size", "type": "desire",
        "theme": "comfort", "product_area": "bras", "confidence": "high",
        "source_url": "https://competitor.com/p", "capture_id": "cap-1",
    }
    row.update(overrides)
    return row


def _finding_row(**overrides):
    row = {
        "id": "finding-1", "finding_kind": "site_fact", "schema_version": 1,
        "payload": {"fact_type": "pricing", "statement": "Buy 2 get 1 free",
                    "detail": "today only!"},
        "source_url": "https://competitor.com/p", "capture_id": "cap-1", "confidence": "medium",
    }
    row.update(overrides)
    return row


def _storage_not_found_error(message="Object not found"):
    """A REAL `storage3.exceptions.StorageApiError` shaped exactly like Supabase
    Storage's documented "object not found" response (P1-3: `_is_object_missing_error`
    now type-checks for this exact class, so a fake that merely LOOKED like one -- a
    bare `Exception` with `.code`/`.status`/`.message` attributes bolted on -- would no
    longer even reach the classifier; the caller's `except StorageApiError:` wouldn't
    catch it)."""
    return StorageApiError(message, "not_found", "404")


def _storage_other_error(message="Bucket not found", code="resource_not_found", status="404"):
    """A REAL `StorageApiError` that is NOT the exact object-not-found signal (a
    different `.code`, even though `.status` may coincidentally also be a 404) -- must
    propagate rather than be misclassified as a missing OBJECT (P1-3)."""
    return StorageApiError(message, code, status)


class _FakeStore:
    """Records every RPC/read call; mutates an in-memory member-result map so
    `get_verify_sample_members` reflects what `rs_verify_record_integrity` (via
    `integrity_bulk_terminalize`) and `rs_verify_record_outcome` have done so far --
    close enough to the real RPCs' effects to exercise `run_verify`'s own re-read logic
    without needing a real database."""

    def __init__(self, capture=None, objects=None, download_raises=None, members=None,
                 integrity_bulk_terminalize=None, voc_quotes=None, findings=None):
        self.capture = capture
        self.objects = objects or {}
        self.download_raises = download_raises or {}
        self._members = {m["evidence_id"]: dict(m) for m in (members or [])}
        self.integrity_bulk_terminalize = integrity_bulk_terminalize
        self.voc_quotes = voc_quotes or {}
        self.findings = findings or {}

        self.freeze_calls = []
        self.download_calls = []
        self.integrity_calls = []
        self.outcome_calls = []
        self.finalize_calls = []

    def rs_verify_freeze_sample(self, job_id, claim_token):
        self.freeze_calls.append((job_id, claim_token))
        return [dict(m) for m in self._members.values()]

    def get_site_capture(self, capture_id):
        return self.capture

    def download_capture_object(self, path):
        self.download_calls.append(path)
        if path in self.download_raises:
            raise self.download_raises[path]
        return self.objects[path]

    def rs_verify_record_integrity(
        self, job_id, claim_token, observed_txt_sha256, observed_html_sha256, txt_missing, html_missing,
    ):
        self.integrity_calls.append({
            "job_id": job_id, "claim_token": claim_token,
            "observed_txt_sha256": observed_txt_sha256, "observed_html_sha256": observed_html_sha256,
            "txt_missing": txt_missing, "html_missing": html_missing,
        })
        if self.integrity_bulk_terminalize:
            for eid in self._members:
                if self._members[eid]["result"] == "pending":
                    self._members[eid]["result"] = self.integrity_bulk_terminalize
        return {"status": self.integrity_bulk_terminalize or "ok"}

    def get_verify_sample_members(self, job_id, result=None):
        rows = [dict(m) for m in self._members.values()]
        if result is not None:
            rows = [r for r in rows if r["result"] == result]
        return rows

    def get_voc_quote(self, quote_id):
        return self.voc_quotes.get(quote_id)

    def get_finding(self, finding_id):
        return self.findings.get(finding_id)

    def rs_verify_record_outcome(self, job_id, claim_token, evidence_kind, evidence_id, result, check_detail):
        self.outcome_calls.append({
            "job_id": job_id, "claim_token": claim_token, "evidence_kind": evidence_kind,
            "evidence_id": evidence_id, "result": result, "check_detail": check_detail,
        })
        self._members[evidence_id]["result"] = result
        return {"result": result}

    def rs_verify_finalize(self, job_id, claim_token):
        self.finalize_calls.append((job_id, claim_token))
        return {"status": "verified", "job_id": job_id}


class _FakeUsage:
    def __init__(self, input_tokens=80, output_tokens=20):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, payload, input_tokens=80, output_tokens=20):
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


def _wire(monkeypatch, fake_store, resp=None, lease_values=True, reserve_result=None, anthropic_client=None):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(verify, "store", fake_store)

    if isinstance(lease_values, list):
        it = iter(lease_values)
        monkeypatch.setattr(verify.jobs, "assert_lease", lambda job_id, claimant: next(it))
    else:
        monkeypatch.setattr(verify.jobs, "assert_lease", lambda job_id, claimant: lease_values)

    if reserve_result is None:
        # 10 cents -- the deployed 'verify' price card's own default ('*': 10), and
        # comfortably covers _worst_case_cents(settings.model) at claude-sonnet-5's rate.
        reserve_result = ReserveResult(ok=True, reserved_est_cents=10, project_scoped=True)
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

    monkeypatch.setattr(verify.budget, "reserve", _fake_reserve)
    monkeypatch.setattr(verify.budget, "settle", _fake_settle)

    anthropic_calls = []
    if anthropic_client is not None:
        monkeypatch.setattr(verify, "Anthropic", lambda api_key: anthropic_client)
    else:
        monkeypatch.setattr(verify, "Anthropic", lambda api_key: _FakeAnthropicClient(resp, anthropic_calls))

    usage_calls = []
    monkeypatch.setattr(verify.usage_reporter, "spend", lambda **kw: usage_calls.append(kw))

    return {
        "reserve_calls": reserve_calls, "settle_calls": settle_calls,
        "anthropic_calls": anthropic_calls, "usage_calls": usage_calls,
    }


def _job(**overrides):
    job = {
        "id": "job-1", "job_kind": "verify", "project_id": "proj-1",
        "params": {"capture_id": "cap-1", "verifier_version": "v1"},
    }
    job.update(overrides)
    return job


def _objects(**overrides):
    objs = {
        "captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8"),
        "captures/rawsha.html": RAW_HTML.encode("utf-8"),
    }
    objs.update(overrides)
    return objs


# ===========================================================================
# Happy paths -- clean voc / finding members, model says supported.
# ===========================================================================

def test_run_verify_clean_voc_member_records_supported(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    resp = _FakeResponse({"verdict": "supported", "rationale": "the review text says exactly this"})
    hooks = _wire(monkeypatch, fake_store, resp)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["members_frozen"] == 1
    assert result["supported"] == 1
    assert result["unsupported"] == 0
    assert result["abstained"] == 0
    assert len(fake_store.outcome_calls) == 1
    outcome = fake_store.outcome_calls[0]
    assert outcome["evidence_kind"] == "voc"
    assert outcome["evidence_id"] == "quote-1"
    assert outcome["result"] == "supported"
    assert outcome["check_detail"]["model"] == verify.settings.model
    assert outcome["check_detail"]["verifier_version"] == "v1"
    assert len(hooks["anthropic_calls"]) == 1
    assert fake_store.finalize_calls == [("job-1", "claimant-1")]


def test_run_verify_finding_member_grounded_records_supported(monkeypatch):
    """LEGACY fallback path: `_finding_row()`'s default payload carries no `evidence` key
    at all (only `detail`), so this exercises the OLD `detail`-based check -- kept
    passing to prove the legacy fallback still grounds and proceeds to the label pass
    exactly as before this change."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("finding", "finding-1")],
        findings={"finding-1": _finding_row()},
    )
    resp = _FakeResponse({"verdict": "supported", "rationale": "the page says buy 2 get 1 free"})
    hooks = _wire(monkeypatch, fake_store, resp)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["supported"] == 1
    outcome = fake_store.outcome_calls[0]
    assert outcome["evidence_kind"] == "finding"
    assert outcome["evidence_id"] == "finding-1"
    assert outcome["result"] == "supported"
    assert len(hooks["anthropic_calls"]) == 1


def test_run_verify_finding_evidence_verbatim_grounds_and_proceeds_to_label_pass(monkeypatch):
    """First-class `payload.evidence` path (the whole point of this change): a finding
    minted with a verbatim `evidence` span verifies against the FRESHLY re-downloaded
    canonical text and proceeds to the adversarial label pass -- never abstains just
    because `detail` itself isn't a verbatim excerpt."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("finding", "finding-1")],
        findings={"finding-1": _finding_row(
            payload={
                "fact_type": "pricing", "statement": "Buy 2 get 1 free",
                "evidence": "today only!",
                "detail": "the model's own non-verbatim elaboration, not on the page",
            },
        )},
    )
    resp = _FakeResponse({"verdict": "supported", "rationale": "the page says buy 2 get 1 free"})
    hooks = _wire(monkeypatch, fake_store, resp)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["supported"] == 1
    assert result["abstained"] == 0
    outcome = fake_store.outcome_calls[0]
    assert outcome["evidence_kind"] == "finding"
    assert outcome["result"] == "supported"
    assert len(hooks["anthropic_calls"]) == 1


# ===========================================================================
# Deterministic verbatim/grounding failure -- unsupported, WITHOUT a paid call.
# ===========================================================================

def test_run_verify_quote_no_longer_verbatim_records_unsupported_without_paid_call(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row(quote="this text is nowhere in the review anymore")},
    )
    hooks = _wire(monkeypatch, fake_store, resp=None)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["unsupported"] == 1
    assert result["supported"] == 0
    outcome = fake_store.outcome_calls[0]
    assert outcome["result"] == "unsupported"
    assert outcome["check_detail"]["reason"] == "verbatim_failed"
    assert outcome["check_detail"]["checked_against"] == "review_text"
    # No paid call at all -- the reserve/Anthropic path is never touched.
    assert hooks["reserve_calls"] == []
    assert hooks["anthropic_calls"] == []


def test_run_verify_finding_evidence_mismatch_records_unsupported_without_paid_call(monkeypatch):
    """The evidence-bearing counterpart of the quote test above -- the KNOWN GAP this
    change closes. A finding whose persisted `payload.evidence` is a non-blank string
    but is NO LONGER verbatim-present in the freshly re-downloaded canonical text is a
    genuine anti-fabrication finding (the page changed, or the row was corrupted), so it
    is scored `'unsupported'` deterministically -- never `'abstained'`, and never via the
    paid label pass."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("finding", "finding-1")],
        findings={"finding-1": _finding_row(
            payload={
                "fact_type": "pricing", "statement": "Buy 2 get 1 free",
                "evidence": "this exact sentence is nowhere on the page anymore",
                "detail": "",
            },
        )},
    )
    hooks = _wire(monkeypatch, fake_store, resp=None)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["unsupported"] == 1
    assert result["abstained"] == 0
    outcome = fake_store.outcome_calls[0]
    assert outcome["result"] == "unsupported"
    assert outcome["check_detail"]["reason"] == "verbatim_failed"
    assert outcome["check_detail"]["checked_against"] == "canonical_text"
    # No paid call at all -- the reserve/Anthropic path is never touched, same discipline
    # as the quote path's deterministic refutation.
    assert hooks["reserve_calls"] == []
    assert hooks["anthropic_calls"] == []


def test_run_verify_finding_not_grounded_abstains_without_paid_call(monkeypatch):
    """P1-2 / LEGACY fallback (scenario c): a finding with NO `evidence` key at all --
    only `detail`, which is NOT a verbatim excerpt of the canonical text -- has nothing
    authoritative to re-ground against (0 such rows exist post-migration; this stays
    robust regardless). "Cannot verify" is ABSTAIN, never a false 'unsupported'
    refutation -- absence of `evidence` must NEVER be reinterpreted as a refutation."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("finding", "finding-1")],
        findings={"finding-1": _finding_row(
            payload={"fact_type": "pricing", "statement": "x", "detail": "not on the page at all"},
        )},
    )
    hooks = _wire(monkeypatch, fake_store, resp=None)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    assert result["unsupported"] == 0
    outcome = fake_store.outcome_calls[0]
    assert outcome["result"] == "abstained"
    assert outcome["check_detail"]["reason"] == "finding_evidence_not_recoverable"
    assert outcome["check_detail"]["checked_against"] == "canonical_text"
    assert hooks["anthropic_calls"] == []


@pytest.mark.parametrize("bad_evidence", [
    None,     # JSON null -- absent evidence, must never be coerced/trusted.
    42,       # a number -- wrong type entirely, never coerced/trusted.
    "",       # empty string -- blank, same as absent.
    "   ",    # whitespace-only string -- blank after strip(), same as absent.
])
def test_run_verify_finding_malformed_evidence_falls_back_to_detail_and_abstains(monkeypatch, bad_evidence):
    """The `evidence_raw.strip() if isinstance(evidence_raw, str) else ""` guard in
    `_check_member`: JSON null, a non-string (a number), and an empty/whitespace string
    must ALL fall back to the LEGACY `detail`-based check exactly like a fully-absent
    `evidence` key -- never crash (`.strip()` on a non-string would raise `AttributeError`
    if the isinstance guard were missing), and never be scored as a verbatim MISMATCH
    (`'unsupported'`) just because the malformed value isn't trusted. When the fallback
    `detail` also fails to verify, the result is `'abstained'` -- proving malformed/absent
    `evidence` is never reinterpreted as a refutation."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("finding", "finding-1")],
        findings={"finding-1": _finding_row(
            payload={
                "fact_type": "pricing", "statement": "x",
                "evidence": bad_evidence,
                "detail": "not on the page at all",
            },
        )},
    )
    hooks = _wire(monkeypatch, fake_store, resp=None)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    assert result["unsupported"] == 0
    outcome = fake_store.outcome_calls[0]
    assert outcome["result"] == "abstained"
    assert outcome["check_detail"]["reason"] == "finding_evidence_not_recoverable"
    assert outcome["check_detail"]["checked_against"] == "canonical_text"
    assert hooks["anthropic_calls"] == []


def test_run_verify_finding_blank_detail_at_mint_time_still_grounds_and_scores_unsupported(monkeypatch):
    """Contrast case: when `detail` genuinely WAS the verbatim evidence (blank `detail`
    at mint time -- see `worker.extract._mint_findings`), the deterministic check DOES
    ground successfully and the label pass runs; if the MODEL then says unsupported,
    that is a real 'unsupported', not an abstain -- P1-2 only changes the FAILED-grounding
    path, never the passed-grounding path."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("finding", "finding-1")],
        # "today only!" IS verbatim-present in RAW_HTML's hero div.
        findings={"finding-1": _finding_row(
            payload={"fact_type": "pricing", "statement": "x", "detail": "today only!"},
        )},
    )
    resp = _FakeResponse({"verdict": "unsupported", "rationale": "doesn't establish pricing"})
    hooks = _wire(monkeypatch, fake_store, resp)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["unsupported"] == 1
    assert result["abstained"] == 0
    assert len(hooks["anthropic_calls"]) == 1


# ===========================================================================
# Model positively says unsupported.
# ===========================================================================

def test_run_verify_model_says_unsupported_records_unsupported(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    resp = _FakeResponse({"verdict": "unsupported", "rationale": "the type label doesn't fit"})
    _wire(monkeypatch, fake_store, resp)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["unsupported"] == 1
    assert result["supported"] == 0
    assert result["abstained"] == 0
    assert fake_store.outcome_calls[0]["result"] == "unsupported"


def test_run_verify_model_says_ambiguous_abstains(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    resp = _FakeResponse({"verdict": "ambiguous", "rationale": "could be read either way"})
    _wire(monkeypatch, fake_store, resp)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    outcome = fake_store.outcome_calls[0]
    assert outcome["result"] == "abstained"
    assert outcome["check_detail"]["reason"] == "ambiguous"


# ===========================================================================
# Provider/infra failure during the label call -- abstained, NEVER unsupported.
# ===========================================================================

def test_run_verify_provider_error_abstains_and_settles_worst_case_without_orphan(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    hooks = _wire(
        monkeypatch, fake_store,
        anthropic_client=_RaisingAnthropicClient(RuntimeError("connection reset by peer")),
    )

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    assert result["unsupported"] == 0  # NEVER a false 'unsupported' out of an infra failure.
    outcome = fake_store.outcome_calls[0]
    assert outcome["result"] == "abstained"
    assert outcome["check_detail"]["reason"] == "provider_error"

    # Exactly one settle, at the WORST CASE (reserved_est) -- never orphaned, never
    # under-counted (a partial/mid-stream failure may have billed real tokens).
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == hooks["settle_calls"][0]["reserved_est"]
    assert hooks["settle_calls"][0]["actual_cents"] == 10
    assert hooks["settle_calls"][0]["report_usage"] is False
    # No usage.spend() call -- the call never returned a real response to report tokens from.
    assert hooks["usage_calls"] == []
    # finalize still runs -- one member's provider error doesn't abort the whole job.
    assert fake_store.finalize_calls == [("job-1", "claimant-1")]


def test_run_verify_malformed_model_response_abstains(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )

    # A response whose content is not valid JSON.
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

    _wire(monkeypatch, fake_store, anthropic_client=_BadClient())

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    assert fake_store.outcome_calls[0]["check_detail"]["reason"] == "malformed_response"


@pytest.mark.parametrize("payload", [
    {"verdict": "yes", "rationale": "looks right"},  # verdict outside the enum.
    {"verdict": "supported"},  # rationale missing entirely.
    {"verdict": "supported", "rationale": "   "},  # rationale blank/whitespace-only.
    {"verdict": "supported", "rationale": 12345},  # rationale not a string.
    [1, 2, 3],  # not a dict at all.
    "just a string",  # not a dict at all.
])
def test_run_verify_malformed_label_shape_abstains_without_raising(monkeypatch, payload):
    """P1-1: well-formed JSON (parses fine) but the WRONG shape -- a verdict outside
    LABEL_VERDICTS, a missing/blank/non-string rationale, or not even a dict -- must
    never raise (no unhandled `.get()` on a non-dict) and must never be miscounted as a
    real 'unsupported'."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    resp = _FakeResponse(payload)
    _wire(monkeypatch, fake_store, resp)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    assert result["unsupported"] == 0
    assert result["supported"] == 0
    outcome = fake_store.outcome_calls[0]
    assert outcome["result"] == "abstained"
    assert outcome["check_detail"]["reason"] == "malformed_response"


def test_run_verify_incomplete_provider_response_abstains_and_settles(monkeypatch):
    """P1-4: the call raises nothing, but the response object is missing/incomplete
    (here: no `.usage` attribute at all) -- must abstain (never crash on `resp.usage.
    input_tokens`) AND settle the reservation at the worst case, with retry, so nothing
    is left orphaned."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )

    class _NoUsageResponse:
        def __init__(self):
            self.content = [_FakeBlock(json.dumps({"verdict": "supported", "rationale": "x"}))]
            # Deliberately no .usage attribute at all.

    class _IncompleteClient:
        @property
        def messages(self):
            return self

        def stream(self, **kwargs):
            return _FakeStreamCtx(_NoUsageResponse())

    hooks = _wire(monkeypatch, fake_store, anthropic_client=_IncompleteClient())

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    assert result["unsupported"] == 0
    outcome = fake_store.outcome_calls[0]
    assert outcome["result"] == "abstained"
    assert outcome["check_detail"]["reason"] == "incomplete_response"

    # Settled at the WORST CASE (reserved_est), never orphaned -- no usage.spend() call
    # either, since there is no real token count to report.
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == hooks["settle_calls"][0]["reserved_est"]
    assert hooks["settle_calls"][0]["actual_cents"] == 10
    assert hooks["settle_calls"][0]["report_usage"] is False
    assert hooks["usage_calls"] == []
    assert fake_store.finalize_calls == [("job-1", "claimant-1")]


def test_run_verify_none_provider_response_abstains_and_settles(monkeypatch):
    """P1-4, the more extreme case: `stream.get_final_message()` itself returns `None`
    without raising."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )

    class _NoneClient:
        @property
        def messages(self):
            return self

        def stream(self, **kwargs):
            return _FakeStreamCtx(None)

    hooks = _wire(monkeypatch, fake_store, anthropic_client=_NoneClient())

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    assert fake_store.outcome_calls[0]["check_detail"]["reason"] == "incomplete_response"
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 10


class _RaisingContentIterable:
    """Passes the UPFRONT `iter(...)` well-formedness check (calling `.__iter__()`
    itself never raises -- it just returns a fresh generator) but raises when actually
    ITERATED (a lazy stream that breaks partway through) -- exactly the P1-B gap: an
    upfront `iter()` probe cannot prove a later `for` loop over the same object won't
    raise, because each call to `__iter__()` here returns a BRAND NEW generator that
    only fails once driven."""

    def __iter__(self):
        def _gen():
            raise RuntimeError("content stream broke mid-iteration")
            yield  # pragma: no cover -- unreachable; makes this function a generator.
        return _gen()


def test_run_verify_content_iteration_raises_abstains_and_settles_no_orphan(monkeypatch):
    """P1-B: `.content` passes the upfront `iter()` check but raises when the `for` loop
    actually drives it -- must be caught by the guarded post-call extraction block, settle
    at the worst case (no orphan), and abstain -- never let this raise out of
    `run_verify` entirely, never a false 'unsupported'."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )

    class _RaisingContentResponse:
        def __init__(self):
            self.usage = _FakeUsage()
            self.content = _RaisingContentIterable()

    class _RaisingContentClient:
        @property
        def messages(self):
            return self

        def stream(self, **kwargs):
            return _FakeStreamCtx(_RaisingContentResponse())

    hooks = _wire(monkeypatch, fake_store, anthropic_client=_RaisingContentClient())

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    assert result["unsupported"] == 0
    outcome = fake_store.outcome_calls[0]
    assert outcome["result"] == "abstained"
    assert outcome["check_detail"]["reason"] == "incomplete_response"

    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == hooks["settle_calls"][0]["reserved_est"]
    assert hooks["settle_calls"][0]["actual_cents"] == 10
    assert hooks["usage_calls"] == []
    assert fake_store.finalize_calls == [("job-1", "claimant-1")]


def test_run_verify_non_string_block_text_abstains_and_settles_no_orphan(monkeypatch):
    """P1-B: a content block whose `.text` is not a string (e.g. an int) must not raise
    out of the `"".join(...)` text-extraction step -- caught by the guarded block, settled
    at the worst case, abstained."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )

    class _NonStringTextBlock:
        type = "text"
        text = 12345  # not a string

    class _NonStringTextResponse:
        def __init__(self):
            self.usage = _FakeUsage()
            self.content = [_NonStringTextBlock()]

    class _NonStringTextClient:
        @property
        def messages(self):
            return self

        def stream(self, **kwargs):
            return _FakeStreamCtx(_NonStringTextResponse())

    hooks = _wire(monkeypatch, fake_store, anthropic_client=_NonStringTextClient())

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    outcome = fake_store.outcome_calls[0]
    assert outcome["check_detail"]["reason"] == "incomplete_response"
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 10


def test_run_verify_budget_unavailable_abstains(monkeypatch):
    """A model with no configured USD/MTok rate -- the worst-case guard fails before any
    call is made. Never crashes the whole job; just this member abstains."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    hooks = _wire(monkeypatch, fake_store, resp=_FakeResponse({"verdict": "supported", "rationale": "x"}))
    monkeypatch.setenv("RESEARCH_MODEL", "some-unpriced-model")

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    outcome = fake_store.outcome_calls[0]
    assert outcome["result"] == "abstained"
    assert outcome["check_detail"]["reason"] == "budget_unavailable"
    assert hooks["anthropic_calls"] == []
    # Reservation released (settle actual=0), never orphaned.
    assert len(hooks["settle_calls"]) == 1
    assert hooks["settle_calls"][0]["actual_cents"] == 0


# ===========================================================================
# Integrity -- strict one-of shape; a present object hashes to the observed value; a
# missing object reports missing=True/observed=None.
# ===========================================================================

def test_run_verify_integrity_present_object_hashes_to_observed_value(monkeypatch):
    import hashlib

    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    _wire(monkeypatch, fake_store, resp=_FakeResponse({"verdict": "supported", "rationale": "x"}))

    verify.run_verify(_job(), "claimant-1")

    assert len(fake_store.integrity_calls) == 1
    call = fake_store.integrity_calls[0]
    assert call["job_id"] == "job-1"
    assert call["claim_token"] == "claimant-1"
    assert call["txt_missing"] is False
    assert call["html_missing"] is False
    assert call["observed_txt_sha256"] == hashlib.sha256(CANONICAL_TEXT.encode("utf-8")).hexdigest()
    assert call["observed_html_sha256"] == hashlib.sha256(RAW_HTML.encode("utf-8")).hexdigest()
    # Strict one-of: a present object's missing flag is False AND a real 64-hex hash.
    assert len(call["observed_txt_sha256"]) == 64
    assert len(call["observed_html_sha256"]) == 64


def test_run_verify_integrity_missing_object_reports_missing_true_observed_none(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8")},
        download_raises={"captures/rawsha.html": _storage_not_found_error()},
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
        integrity_bulk_terminalize="integrity_failed",
    )
    _wire(monkeypatch, fake_store, resp=None)

    result = verify.run_verify(_job(), "claimant-1")

    call = fake_store.integrity_calls[0]
    # Strict one-of: the missing object gets observed=None, missing=True.
    assert call["observed_html_sha256"] is None
    assert call["html_missing"] is True
    # The present object is unaffected.
    assert call["txt_missing"] is False
    assert call["observed_txt_sha256"] is not None

    # The RPC's own bulk-terminalize (simulated here) already resolved the member --
    # run_verify must never call record_outcome for it, and must count it correctly.
    assert fake_store.outcome_calls == []
    assert result["integrity_failed"] == 1
    assert result["supported"] == 0
    assert result["unsupported"] == 0
    assert result["abstained"] == 0
    # finalize still runs.
    assert fake_store.finalize_calls == [("job-1", "claimant-1")]


def test_run_verify_download_infra_failure_propagates(monkeypatch):
    """A non-Storage failure (transport/network, ...) never even reaches the
    missing-object classifier and must propagate, never be silently recorded as
    'missing'."""
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8")},
        download_raises={"captures/rawsha.html": RuntimeError("connection reset by peer")},
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    _wire(monkeypatch, fake_store, resp=None)

    with pytest.raises(RuntimeError, match="connection reset"):
        verify.run_verify(_job(), "claimant-1")

    assert fake_store.integrity_calls == []
    assert fake_store.finalize_calls == []


def test_run_verify_storage_error_with_wrong_code_propagates(monkeypatch):
    """P1-3: a REAL `StorageApiError` whose code/status do NOT exactly match the
    documented object-not-found signal (e.g. a bucket-level error that also happens to
    carry a 404 status) must PROPAGATE, never be misclassified as 'this object is
    missing' -- the earlier broad substring match would have swallowed this."""
    fake_store = _FakeStore(
        capture=_capture(),
        objects={"captures/canonsha.txt": CANONICAL_TEXT.encode("utf-8")},
        download_raises={"captures/rawsha.html": _storage_other_error()},
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    _wire(monkeypatch, fake_store, resp=None)

    with pytest.raises(StorageApiError):
        verify.run_verify(_job(), "claimant-1")

    assert fake_store.integrity_calls == []
    assert fake_store.finalize_calls == []


# ===========================================================================
# 0-member sample -- still authenticates the capture (integrity + finalize).
# ===========================================================================

def test_run_verify_zero_member_sample_still_records_integrity_and_finalizes(monkeypatch):
    fake_store = _FakeStore(capture=_capture(), objects=_objects(), members=[])
    _wire(monkeypatch, fake_store, resp=None)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["members_frozen"] == 0
    assert result["supported"] == 0
    assert result["unsupported"] == 0
    assert result["abstained"] == 0
    assert len(fake_store.integrity_calls) == 1
    assert fake_store.finalize_calls == [("job-1", "claimant-1")]
    assert fake_store.outcome_calls == []


# ===========================================================================
# Lease fencing.
# ===========================================================================

def test_run_verify_lease_lost_before_record_outcome_aborts_no_further_outcomes(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        # Not verbatim-present -- fails deterministically, no paid call, so the only
        # assert_lease calls are (1) before record_integrity and (2) before record_outcome.
        voc_quotes={"quote-1": _voc_row(quote="nowhere in the review text")},
    )
    hooks = _wire(monkeypatch, fake_store, resp=None, lease_values=[True, False])

    with pytest.raises(RuntimeError, match="lease"):
        verify.run_verify(_job(), "claimant-1")

    assert fake_store.outcome_calls == []
    assert fake_store.finalize_calls == []
    assert hooks["anthropic_calls"] == []


def test_run_verify_lease_lost_before_reserving_spend_raises(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    # Pre-integrity True, then False right before the label pass's own reserve() call.
    hooks = _wire(monkeypatch, fake_store, resp=None, lease_values=[True, False])

    with pytest.raises(RuntimeError, match="lease"):
        verify.run_verify(_job(), "claimant-1")

    assert hooks["reserve_calls"] == []
    assert fake_store.outcome_calls == []


def test_run_verify_lease_lost_before_record_integrity_raises(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    _wire(monkeypatch, fake_store, resp=None, lease_values=False)

    with pytest.raises(RuntimeError, match="lease"):
        verify.run_verify(_job(), "claimant-1")

    assert fake_store.integrity_calls == []


# ===========================================================================
# Budget: reserve worst-case then settle actual; usage_reporter.spend gets real token
# counts; settle report_usage=False (same discipline as run_collect).
# ===========================================================================

def test_run_verify_reserve_worst_case_then_settle_actual_reports_real_tokens(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    resp = _FakeResponse({"verdict": "supported", "rationale": "yes"}, input_tokens=500, output_tokens=40)
    hooks = _wire(monkeypatch, fake_store, resp)

    verify.run_verify(_job(), "claimant-1")

    assert len(hooks["reserve_calls"]) == 1
    job_arg, ref_arg, claimant_arg, connector_arg = hooks["reserve_calls"][0]
    assert claimant_arg == "claimant-1"
    assert connector_arg is None
    assert ref_arg == "verify:job-1:voc:quote-1:claimant-1"

    assert len(hooks["settle_calls"]) == 1
    settle = hooks["settle_calls"][0]
    assert settle["report_usage"] is False
    assert settle["reserved_est"] == 10
    assert settle["actual_cents"] == verify._actual_cents(500, 40)

    assert len(hooks["usage_calls"]) == 1
    usage_call = hooks["usage_calls"][0]
    assert usage_call["input_tokens"] == 500
    assert usage_call["output_tokens"] == 40
    assert usage_call["model"] == verify.settings.model


def test_run_verify_reserve_skip_replay_abstains_without_calling_anthropic(monkeypatch):
    """P1 (round 3): a reserve 'skip' means this exact ref was already reserved by a
    prior attempt that could have crashed before ever settling -- the skip branch now
    reconciles that reservation at its CEILING (via `_settle_with_retry`, provably safe
    since `rs_settle_call` is idempotent server-side and a 'skip' guarantees a matching
    reserve exists) BEFORE abstaining, so the reservation is never left orphaned."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    skip_result = ReserveResult(ok=False, reserved_est_cents=10, project_scoped=True)
    hooks = _wire(monkeypatch, fake_store, resp=None, reserve_result=skip_result)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    outcome = fake_store.outcome_calls[0]
    assert outcome["result"] == "abstained"
    assert outcome["check_detail"]["reason"] == "reserve_skip_replay"
    assert hooks["anthropic_calls"] == []

    # The reservation is reconciled at the CEILING (reserved_est_cents), not orphaned --
    # this is either a genuine no-op (the prior attempt already settled it) or closes a
    # real leak (it never did), and either way is settled through _settle_with_retry so
    # a transient DB blip on the reconciliation itself doesn't re-orphan it.
    assert len(hooks["settle_calls"]) == 1
    settle = hooks["settle_calls"][0]
    assert settle["actual_cents"] == 10
    assert settle["reserved_est"] == 10
    assert settle["report_usage"] is False
    assert fake_store.finalize_calls == [("job-1", "claimant-1")]


# ===========================================================================
# Finalize called once at the end.
# ===========================================================================

def test_run_verify_finalize_called_once_with_job_id_and_claimant(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1"), _member("finding", "finding-1")],
        voc_quotes={"quote-1": _voc_row()},
        findings={"finding-1": _finding_row()},
    )
    resp = _FakeResponse({"verdict": "supported", "rationale": "yes"})
    verify_result_wire = _wire(monkeypatch, fake_store, resp)

    result = verify.run_verify(_job(), "claimant-1")

    assert fake_store.finalize_calls == [("job-1", "claimant-1")]
    assert result["verdict"] == {"status": "verified", "job_id": "job-1"}
    assert len(verify_result_wire["anthropic_calls"]) == 2


# ===========================================================================
# Misc -- capture_id trust boundary (P1-7), missing capture, unknown evidence_kind,
# missing evidence row.
# ===========================================================================

def test_run_verify_capture_id_uses_job_column_when_params_lacks_it(monkeypatch):
    """`verify_capture_id` (the job row's own column) is used when `params` carries no
    `capture_id` at all -- the ordinary shape a real claimed job row has."""
    fake_store = _FakeStore(capture=_capture(), objects=_objects(), members=[])
    _wire(monkeypatch, fake_store, resp=None)

    job = {"id": "job-1", "job_kind": "verify", "params": {"verifier_version": "v1"},
           "verify_capture_id": "cap-1"}
    result = verify.run_verify(job, "claimant-1")

    assert result["members_frozen"] == 0
    assert fake_store.finalize_calls == [("job-1", "claimant-1")]


def test_run_verify_capture_id_column_is_authoritative_when_both_agree(monkeypatch):
    """Both `params.capture_id` and `verify_capture_id` present and IDENTICAL -- no
    mismatch, proceeds normally using that capture id."""
    fake_store = _FakeStore(capture=_capture(), objects=_objects(), members=[])
    _wire(monkeypatch, fake_store, resp=None)

    job = {"id": "job-1", "job_kind": "verify",
           "params": {"capture_id": "cap-1", "verifier_version": "v1"},
           "verify_capture_id": "cap-1"}
    result = verify.run_verify(job, "claimant-1")

    assert result["members_frozen"] == 0
    assert fake_store.finalize_calls == [("job-1", "claimant-1")]


def test_run_verify_capture_id_mismatch_between_params_and_column_raises(monkeypatch):
    """P1-7: `params.capture_id` is untrusted caller-supplied data; `verify_capture_id`
    (the job row's own column) is what the RPCs actually gate/authenticate against
    server-side. If the two DISAGREE, this must raise rather than silently downloading
    and hashing whichever one this code happened to pick -- proceeding could falsely mark
    an unrelated capture's whole sample 'integrity_failed', or (worse) authenticate the
    WRONG capture entirely."""
    fake_store = _FakeStore(capture=_capture(), objects=_objects(), members=[])
    _wire(monkeypatch, fake_store, resp=None)

    job = {"id": "job-1", "job_kind": "verify",
           "params": {"capture_id": "cap-ATTACKER-SUPPLIED", "verifier_version": "v1"},
           "verify_capture_id": "cap-1"}

    with pytest.raises(ValueError, match="does not match"):
        verify.run_verify(job, "claimant-1")

    # Refuses BEFORE ever touching freeze/download/integrity -- no partial work done
    # against either capture.
    assert fake_store.freeze_calls == []
    assert fake_store.download_calls == []
    assert fake_store.integrity_calls == []


def test_run_verify_capture_not_found_raises(monkeypatch):
    fake_store = _FakeStore(capture=None, objects=_objects(), members=[])
    _wire(monkeypatch, fake_store, resp=None)

    with pytest.raises(ValueError, match="cap-1"):
        verify.run_verify(_job(), "claimant-1")


def test_run_verify_unknown_evidence_kind_abstains(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("mystery_kind", "thing-1")],
    )
    _wire(monkeypatch, fake_store, resp=None)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    assert fake_store.outcome_calls[0]["check_detail"]["reason"] == "unknown_evidence_kind"


def test_run_verify_evidence_row_missing_abstains(monkeypatch):
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-deleted")],
        voc_quotes={},  # the quote row referenced by the sample member no longer exists.
    )
    _wire(monkeypatch, fake_store, resp=None)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    assert fake_store.outcome_calls[0]["check_detail"]["reason"] == "evidence_row_missing"


# ===========================================================================
# Pure helpers.
# ===========================================================================

def test_verbatim_ok_true_for_normalized_substring():
    assert verify._verbatim_ok("it’s great", "text says it's great here") is True


def test_verbatim_ok_false_for_absent_text():
    assert verify._verbatim_ok("not present anywhere", "some other text") is False


def test_verbatim_ok_false_for_empty_candidate():
    assert verify._verbatim_ok("", "some text") is False


def test_build_label_schema_shape():
    schema = verify.build_label_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["verdict"]["enum"] == verify.LABEL_VERDICTS
    assert set(schema["required"]) == {"verdict", "rationale"}


def test_is_object_missing_error_recognises_exact_not_found_signal():
    assert verify._is_object_missing_error(_storage_not_found_error()) is True


def test_is_object_missing_error_rejects_generic_failure():
    assert verify._is_object_missing_error(RuntimeError("connection reset by peer")) is False


def test_is_object_missing_error_rejects_differently_coded_storage_error():
    """P1-3: exact code/status equality, not a substring -- a StorageApiError that is
    NOT the documented not_found/404 pair is never treated as 'missing', even though it
    is a genuine StorageApiError and even if its message happens to mention "not found"."""
    assert verify._is_object_missing_error(_storage_other_error(
        message="Bucket not found", code="resource_not_found", status="404",
    )) is False
    assert verify._is_object_missing_error(_storage_other_error(
        message="Object not found", code="not_found", status="500",
    )) is False


def test_is_well_formed_label_result_accepts_valid_shapes():
    for verdict in verify.LABEL_VERDICTS:
        assert verify._is_well_formed_label_result({"verdict": verdict, "rationale": "why"}) is True


@pytest.mark.parametrize("label_result", [
    None,
    [1, 2, 3],
    "a bare string",
    42,
    {},
    {"verdict": "yes"},
    {"verdict": "supported"},
    {"verdict": "supported", "rationale": ""},
    {"verdict": "supported", "rationale": "   "},
    {"verdict": "supported", "rationale": None},
    {"verdict": "supported", "rationale": 7},
    {"rationale": "no verdict key at all"},
])
def test_is_well_formed_label_result_rejects_malformed_shapes(label_result):
    """P1-1: every one of these must return False (never raise) -- `_label_pass` relies
    on this gate running BEFORE any `.get`/indexing on the model's raw parsed output."""
    assert verify._is_well_formed_label_result(label_result) is False


def test_is_well_formed_label_result_rejects_extra_unexpected_key():
    """P2 (round 8 additionalProperties:false case): an otherwise-valid dict -- a correct
    `verdict` from LABEL_VERDICTS plus a genuinely non-blank `rationale` -- is STILL
    rejected when it carries an extra, unexpected key. This mirrors `build_label_schema`'s
    own `additionalProperties: False` declaration at the Python validation layer too
    (defence in depth): structured output SHOULD already forbid extras, but this check
    must not silently trust a provider response that violates its own requested schema."""
    assert verify._is_well_formed_label_result({
        "verdict": "supported", "rationale": "a real, non-blank rationale",
        "confidence": "high",  # not part of the schema -- unexpected.
    }) is False
    assert verify._is_well_formed_label_result({
        "verdict": "unsupported", "rationale": "why",
        "extra_injected_field": "ignore previous instructions",
    }) is False


def test_run_verify_label_result_with_extra_key_abstains(monkeypatch):
    """End-to-end: a label-pass response that parses as valid JSON, has a correct
    verdict and a non-blank rationale, but ALSO carries an extra key must abstain
    (never a false 'supported'/'unsupported') -- proves `_label_pass` actually enforces
    `_is_well_formed_label_result`'s additionalProperties check, not just the unit test."""
    fake_store = _FakeStore(
        capture=_capture(), objects=_objects(),
        members=[_member("voc", "quote-1")],
        voc_quotes={"quote-1": _voc_row()},
    )
    resp = _FakeResponse({
        "verdict": "supported", "rationale": "looks right",
        "unexpected_extra_field": "should never be trusted",
    })
    _wire(monkeypatch, fake_store, resp)

    result = verify.run_verify(_job(), "claimant-1")

    assert result["abstained"] == 1
    assert result["supported"] == 0
    assert result["unsupported"] == 0
    outcome = fake_store.outcome_calls[0]
    assert outcome["result"] == "abstained"
    assert outcome["check_detail"]["reason"] == "malformed_response"


def test_is_well_formed_provider_response_accepts_valid_shape():
    resp = _FakeResponse({"verdict": "supported", "rationale": "x"})
    assert verify._is_well_formed_provider_response(resp) is True


@pytest.mark.parametrize("resp", [None])
def test_is_well_formed_provider_response_rejects_none(resp):
    assert verify._is_well_formed_provider_response(resp) is False


def test_is_well_formed_provider_response_rejects_missing_usage():
    class _NoUsage:
        content = [_FakeBlock("{}")]

    assert verify._is_well_formed_provider_response(_NoUsage()) is False


def test_is_well_formed_provider_response_rejects_non_int_tokens():
    class _BadUsage:
        input_tokens = "not-a-number"
        output_tokens = 10

    class _Resp:
        usage = _BadUsage()
        content = [_FakeBlock("{}")]

    assert verify._is_well_formed_provider_response(_Resp()) is False


def test_is_well_formed_provider_response_rejects_missing_content():
    class _NoContent:
        usage = _FakeUsage()

    assert verify._is_well_formed_provider_response(_NoContent()) is False


def test_worst_case_cents_accounts_for_system_prompt_framing_and_schema(monkeypatch=None):
    """P1-5/P1-A regression: the worst-case estimate must be strictly greater than what
    the two variable char-capped blocks alone would cost at a naive 1-token/char rate --
    i.e. it genuinely includes the fixed system prompt + prompt framing + structured-
    output schema/protocol overhead, not just _MAX_LABEL_CHARS + _MAX_INPUT_CHARS at
    1 token/char (the OLD, insufficient computation)."""
    import math as _math

    input_rate, output_rate = verify.model_rate_usd_per_mtok("claude-sonnet-5")
    naive_blocks_only_dollars = (
        (verify._MAX_LABEL_CHARS + verify._MAX_INPUT_CHARS) * input_rate
        + verify._MAX_TOKENS * output_rate
    ) / 1_000_000
    naive_blocks_only_cents = _math.ceil(naive_blocks_only_dollars * 100)

    actual = verify._worst_case_cents("claude-sonnet-5")

    assert verify._SYSTEM_PROMPT_CHARS > 0
    assert verify._MAX_FRAMING_CHARS > 0
    assert verify._SCHEMA_CHARS > 0
    assert verify._PROTOCOL_OVERHEAD_CHARS > 0
    assert actual > naive_blocks_only_cents
    # Still comfortably within the deployed 'verify' price card's default ceiling (10
    # cents) at the default model -- if this ever fails, the price card (worker/config.py)
    # needs bumping, not this assertion loosening.
    assert actual <= 10


def test_worst_case_cents_uses_the_2x_per_char_conservative_rate():
    """P1-A: 1 token/char is NOT a true upper bound (adversarial Unicode can tokenize to
    more than one token per character) -- `_worst_case_cents` must use the SAME 2x/char
    rate `worker.extract._worst_case_cents` uses, applied to the full worst-case char
    budget (system + framing + label + input + schema + protocol)."""
    input_rate, output_rate = verify.model_rate_usd_per_mtok("claude-sonnet-5")
    worst_chars = (
        verify._SYSTEM_PROMPT_CHARS + verify._MAX_FRAMING_CHARS + verify._MAX_LABEL_CHARS
        + verify._MAX_INPUT_CHARS + verify._SCHEMA_CHARS + verify._PROTOCOL_OVERHEAD_CHARS
    )
    import math as _math
    expected_dollars = (2 * worst_chars * input_rate + verify._MAX_TOKENS * output_rate) / 1_000_000
    expected_cents = _math.ceil(expected_dollars * 100)

    assert verify._worst_case_cents("claude-sonnet-5") == expected_cents


def test_worst_case_cents_covers_a_realistic_high_token_input():
    """P1-A: the reservation must be a genuine ceiling even for input that tokenizes at a
    HIGH rate. Assert the worst-case estimate comfortably covers a realistic 'high'
    actual-usage scenario where every character of the FULL (capped) worst-case input
    costs a full token each -- the OLD, now-proven-insufficient 1-token/char assumption
    -- plus the full output ceiling; the new 2x/char reservation must still exceed it."""
    total_capped_chars = (
        verify._SYSTEM_PROMPT_CHARS + verify._MAX_FRAMING_CHARS + verify._MAX_LABEL_CHARS
        + verify._MAX_INPUT_CHARS + verify._SCHEMA_CHARS + verify._PROTOCOL_OVERHEAD_CHARS
    )
    realistic_high_cents = verify._actual_cents(total_capped_chars, verify._MAX_TOKENS)
    worst_case = verify._worst_case_cents("claude-sonnet-5")

    assert worst_case >= realistic_high_cents
    # And even THIS realistic-high scenario stays under the deployed price card's
    # ceiling, confirming the reservation is a genuine, usable ceiling in practice.
    assert worst_case <= 10


def test_build_label_prompt_truncates_evidence_text_to_max_input_chars():
    """The model-facing prompt is HARD-capped at _MAX_INPUT_CHARS (P1-A) -- this is what
    makes the worst-case reservation a true ceiling: the model can never see more than
    this many evidence characters, regardless of how large the real review/canonical
    text is."""
    huge_text = "x" * (verify._MAX_INPUT_CHARS + 5000)
    prompt = verify._build_label_prompt(verify.EVIDENCE_KIND_VOC, {}, huge_text)
    # The capped run of "x" characters appears, but not the full 5000-char overrun.
    assert ("x" * verify._MAX_INPUT_CHARS) in prompt
    assert ("x" * (verify._MAX_INPUT_CHARS + 1)) not in prompt
