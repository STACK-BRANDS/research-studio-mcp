"""Tests for the spend layer (Phase 1 tasks 1.4-1.5 of the deep-research
plan): `worker/budget.py`'s reserve/settle over the deployed money-migration
(`rs_reserve_spend` / `rs_settle_call`, migration 138) plus `store.py`'s thin
RPC wrappers and the `CostAccrual` accumulator.

No network, no real Supabase, no real Anthropic: `store._client()` is
monkeypatched to a fake Supabase client (mirroring test_jobs.py's
query-builder-chain fake), and `worker.spend_guard.guard` /
`worker.budget.usage_reporter` are stubbed so a test never touches the real
disk-backed spend-guard state file or the real fleet usage endpoint.
"""
import pytest

from worker import budget, store
from worker.budget import BudgetOverspendError, CostAccrual, ReserveResult
from worker.config import settings


# ---------------------------------------------------------------------------
# Fake Supabase client for store.rs_reserve_spend / store.rs_settle_call --
# same shape as test_jobs.py's fake: records every rpc() call's params so a
# test can assert the exact payload the store function built.
# ---------------------------------------------------------------------------

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


class _FakeClient:
    def __init__(self, rpc_response=None):
        self.rpc_response = rpc_response
        self.rpcs = []

    def rpc(self, name, params):
        r = _FakeRpc(name, params, self.rpc_response)
        self.rpcs.append(r)
        return r


def _wire(monkeypatch, rpc_response=None):
    client = _FakeClient(rpc_response=rpc_response)
    monkeypatch.setattr(store, "_client", lambda: client)
    return client


# ---------------------------------------------------------------------------
# 1.4 -- store.rs_reserve_spend / store.rs_settle_call RPC wrappers
# ---------------------------------------------------------------------------

def test_rs_reserve_spend_returns_ok(monkeypatch):
    client = _wire(monkeypatch, rpc_response="ok")
    outcome = store.rs_reserve_spend("job-1", "proj-1", "ref-1", 80, "claimant-1", "ad_library.scrapecreators")
    assert outcome == "ok"
    assert len(client.rpcs) == 1
    assert client.rpcs[0].name == "rs_reserve_spend"
    assert client.rpcs[0].params == {
        "p_job_id": "job-1",
        "p_project_id": "proj-1",
        "p_ref": "ref-1",
        "p_est_cents": 80,
        "p_claimant": "claimant-1",
        "p_connector": "ad_library.scrapecreators",
    }


def test_rs_reserve_spend_returns_skip(monkeypatch):
    _wire(monkeypatch, rpc_response="skip")
    outcome = store.rs_reserve_spend("job-1", "proj-1", "ref-1", 80, "claimant-1")
    assert outcome == "skip"


def test_rs_reserve_spend_omits_connector_when_none(monkeypatch):
    client = _wire(monkeypatch, rpc_response="ok")
    store.rs_reserve_spend("job-1", "proj-1", "ref-1", 25, "claimant-1", connector=None)
    assert "p_connector" not in client.rpcs[0].params


def test_rs_reserve_spend_propagates_rpc_exception(monkeypatch):
    """NOT best-effort: a guard failure inside the RPC (lease revoked,
    project unapproved, ceiling exceeded, ...) must propagate, never
    collapse to a clean return value."""
    class _RaisingClient:
        def rpc(self, name, params):
            raise RuntimeError("ceiling exceeded")

    monkeypatch.setattr(store, "_client", lambda: _RaisingClient())
    with pytest.raises(RuntimeError, match="ceiling exceeded"):
        store.rs_reserve_spend("job-1", "proj-1", "ref-1", 80, "claimant-1")


def test_rs_settle_call_sends_expected_params(monkeypatch):
    client = _wire(monkeypatch, rpc_response=None)
    store.rs_settle_call("ref-1", 42, "claimant-1")
    assert len(client.rpcs) == 1
    assert client.rpcs[0].name == "rs_settle_call"
    assert client.rpcs[0].params == {"p_ref": "ref-1", "p_actual_cents": 42, "p_claimant": "claimant-1"}


def test_rs_settle_call_omits_claimant_when_none(monkeypatch):
    client = _wire(monkeypatch, rpc_response=None)
    store.rs_settle_call("ref-1", 42, claimant=None)
    assert client.rpcs[0].params == {"p_ref": "ref-1", "p_actual_cents": 42}


def test_rs_settle_call_propagates_rpc_exception(monkeypatch):
    class _RaisingClient:
        def rpc(self, name, params):
            raise RuntimeError("no matching reserve")

    monkeypatch.setattr(store, "_client", lambda: _RaisingClient())
    with pytest.raises(RuntimeError, match="no matching reserve"):
        store.rs_settle_call("ref-1", 42)


# ---------------------------------------------------------------------------
# worker.config.Settings.price_cards / price_for
# ---------------------------------------------------------------------------

def test_price_for_wildcard_kinds():
    assert settings.price_for("collect") == 25
    assert settings.price_for("verify") == 10
    assert settings.price_for("synthesize") == 75


def test_price_for_scrape_connectors():
    assert settings.price_for("scrape", "ad_library.scrapecreators") == 80
    assert settings.price_for("scrape", "web.fetch") == 0


def test_price_for_scrape_without_connector_raises():
    """scrape has no "*" wildcard -- its two connectors have wildly
    different cost profiles, so an unrecognised/missing connector must fail
    closed rather than silently under-price."""
    with pytest.raises(KeyError):
        settings.price_for("scrape")


def test_price_for_unknown_job_kind_raises():
    with pytest.raises(KeyError):
        settings.price_for("no-such-kind")


def test_price_cards_env_override(monkeypatch):
    monkeypatch.setenv("RESEARCH_PRICE_CENTS_COLLECT", "999")
    assert settings.price_for("collect") == 999


# ---------------------------------------------------------------------------
# Stubs for budget.reserve()/settle() -- never touch the real disk-backed
# spend_guard state file or the real fleet usage endpoint.
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_guard(monkeypatch):
    """Stub worker.spend_guard.guard(): records call count, never raises
    unless armed to. budget.reserve() must call this as Layer 1, for every
    job, before anything else."""
    calls = {"n": 0}

    def _guard(*args, **kwargs):
        calls["n"] += 1

    monkeypatch.setattr(budget.spend_guard, "guard", _guard)
    return calls


@pytest.fixture
def fake_usage(monkeypatch):
    """Stub the shared UsageReporter's .cost() method (budget.usage_reporter
    is worker.analyze.usage, the same singleton analyze.py reports through)
    so a test never attempts a real network POST."""
    events = []

    def _cost(action, cost, quantity=None, unit=None, store_id=None, meta=None):
        events.append({"action": action, "cost": cost, "store_id": store_id, "meta": meta})

    monkeypatch.setattr(budget.usage_reporter, "cost", _cost)
    return events


def _job(project_id="proj-1", job_kind="collect", job_id="job-1", store_id=None):
    return {"id": job_id, "project_id": project_id, "job_kind": job_kind, "store_id": store_id}


# ---------------------------------------------------------------------------
# 1.4 -- budget.reserve()
# ---------------------------------------------------------------------------

def test_reserve_project_scoped_ok(monkeypatch, fake_guard):
    calls = []

    def _rs_reserve_spend(job_id, project_id, ref, est_cents, claimant, connector=None):
        calls.append((job_id, project_id, ref, est_cents, claimant, connector))
        return "ok"

    monkeypatch.setattr(store, "rs_reserve_spend", _rs_reserve_spend)

    result = budget.reserve(_job(job_kind="collect"), "ref-1", "claimant-1")
    assert isinstance(result, ReserveResult)
    assert bool(result) is True
    assert result.ok is True
    assert result.project_scoped is True
    assert result.reserved_est_cents == 25  # collect's price card
    assert fake_guard["n"] == 1
    assert calls == [("job-1", "proj-1", "ref-1", 25, "claimant-1", None)]


def test_reserve_project_scoped_skip(monkeypatch, fake_guard):
    monkeypatch.setattr(store, "rs_reserve_spend", lambda *a, **k: "skip")

    result = budget.reserve(_job(job_kind="verify"), "ref-1", "claimant-1")
    assert bool(result) is False
    assert result.ok is False
    assert result.project_scoped is True
    assert result.reserved_est_cents == 10  # verify's price card


def test_reserve_project_scoped_unexpected_outcome_raises(monkeypatch, fake_guard):
    monkeypatch.setattr(store, "rs_reserve_spend", lambda *a, **k: "???")
    with pytest.raises(ValueError):
        budget.reserve(_job(), "ref-1", "claimant-1")


def test_reserve_adhoc_skips_ledger_reserve_but_calls_spend_guard(monkeypatch, fake_guard):
    """R3: ad-hoc (project_id=None) jobs never call rs_reserve_spend, but
    spend_guard.guard() (Layer 1) still runs -- it is the only gate for this
    path."""
    called = {"n": 0}

    def _rs_reserve_spend(*a, **k):
        called["n"] += 1
        return "ok"

    monkeypatch.setattr(store, "rs_reserve_spend", _rs_reserve_spend)

    job = _job(project_id=None, job_kind="scrape")
    result = budget.reserve(job, "ref-1", "claimant-1", connector="ad_library.scrapecreators")
    assert bool(result) is True
    assert result.project_scoped is False
    assert result.reserved_est_cents == 80  # scrape/ad_library.scrapecreators price card
    assert called["n"] == 0  # never reached the ledger
    assert fake_guard["n"] == 1  # but spend_guard still ran


def test_reserve_spend_guard_cap_propagates(monkeypatch):
    """A spend_guard cap hit must propagate out of reserve() uncaught, and
    the ledger reserve must never be attempted."""
    from worker.spend_guard import ResearchSpendCapExceeded

    reserve_called = {"n": 0}

    def _raising_guard(*a, **k):
        raise ResearchSpendCapExceeded("hour", 20, 20)

    def _rs_reserve_spend(*a, **k):
        reserve_called["n"] += 1
        return "ok"

    monkeypatch.setattr(budget.spend_guard, "guard", _raising_guard)
    monkeypatch.setattr(store, "rs_reserve_spend", _rs_reserve_spend)

    with pytest.raises(ResearchSpendCapExceeded):
        budget.reserve(_job(), "ref-1", "claimant-1")
    assert reserve_called["n"] == 0


# ---------------------------------------------------------------------------
# 1.4 -- budget.settle()
# ---------------------------------------------------------------------------

def test_settle_project_scoped_calls_rs_settle_call_and_reports_usage(monkeypatch, fake_usage):
    calls = []

    def _rs_settle_call(ref, actual_cents, claimant=None):
        calls.append((ref, actual_cents, claimant))

    monkeypatch.setattr(store, "rs_settle_call", _rs_settle_call)

    budget.settle(_job(job_kind="collect"), "ref-1", 20, "claimant-1", reserved_est=25)

    assert calls == [("ref-1", 20, "claimant-1")]
    assert len(fake_usage) == 1
    assert fake_usage[0]["action"] == "rs-worker/collect"
    assert fake_usage[0]["cost"] == pytest.approx(0.20)


def test_settle_adhoc_skips_rs_settle_call_but_still_reports_usage(monkeypatch, fake_usage):
    """R3: an ad-hoc job never reserved in the ledger, so settle() must not
    call rs_settle_call for it -- but usage reporting still fires."""
    called = {"n": 0}

    def _rs_settle_call(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(store, "rs_settle_call", _rs_settle_call)

    budget.settle(_job(project_id=None, job_kind="scrape"), "ref-1", 60, "claimant-1", reserved_est=80)

    assert called["n"] == 0
    assert len(fake_usage) == 1
    assert fake_usage[0]["cost"] == pytest.approx(0.60)


def test_settle_raises_when_actual_exceeds_reserved(monkeypatch, fake_usage):
    """R2: actual_cents must never exceed reserved_est -- a violation is a
    pricing-card bug and must raise loudly, before any write."""
    called = {"n": 0}
    monkeypatch.setattr(store, "rs_settle_call", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    with pytest.raises(BudgetOverspendError):
        budget.settle(_job(job_kind="verify"), "ref-1", 11, "claimant-1", reserved_est=10)

    # The overspend check runs BEFORE any ledger write or usage report.
    assert called["n"] == 0
    assert len(fake_usage) == 0


def test_settle_actual_equal_to_reserved_is_allowed(monkeypatch, fake_usage):
    monkeypatch.setattr(store, "rs_settle_call", lambda *a, **k: None)
    budget.settle(_job(job_kind="verify"), "ref-1", 10, "claimant-1", reserved_est=10)
    assert len(fake_usage) == 1


def test_settle_report_usage_false_suppresses_usage_event(monkeypatch, fake_usage):
    """A caller that already reported this call at a finer grain (e.g.
    analyze.py's own usage.spend() with real token counts) can suppress
    settle()'s coarse cost() report to avoid double-counting."""
    monkeypatch.setattr(store, "rs_settle_call", lambda *a, **k: None)
    budget.settle(
        _job(job_kind="collect"), "ref-1", 20, "claimant-1", reserved_est=25, report_usage=False,
    )
    assert len(fake_usage) == 0


# ---------------------------------------------------------------------------
# 1.5 -- CostAccrual
# ---------------------------------------------------------------------------

def test_cost_accrual_sums_across_calls():
    accrual = CostAccrual()
    accrual.add(20)
    accrual.add(35)
    accrual.add(0)
    assert accrual.total_cents == 55


def test_cost_accrual_checkpoint_drains_pending_without_affecting_total():
    accrual = CostAccrual()
    accrual.add(10)
    accrual.add(15)
    pending = accrual.checkpoint()
    assert pending == 25
    assert accrual.total_cents == 25  # total is cumulative, unaffected by checkpoint drains

    accrual.add(5)
    pending2 = accrual.checkpoint()
    assert pending2 == 5  # only what accrued since the previous checkpoint
    assert accrual.total_cents == 30


def test_cost_accrual_starts_at_zero():
    accrual = CostAccrual()
    assert accrual.total_cents == 0
    assert accrual.checkpoint() == 0
