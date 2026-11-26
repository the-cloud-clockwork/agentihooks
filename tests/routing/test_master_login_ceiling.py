import pytest

from scripts import claude_quota_balancer as balancer
from scripts.routing import place, reservation
from scripts.routing.settings import FileSettings
from scripts.swarm import capacity

REASON = "reserved for 1 live master: workers stop at 70% of the week"
WORKER = {"AGENTIHOOKS_AGENT_NAME": "engineer@1"}
MASTER = {"AGENTIHOOKS_AGENT_NAME": "master@1"}


@pytest.fixture
def environ(tmp_path, monkeypatch):
    store = FileSettings(tmp_path / "routing-settings.json")
    store.set("master-account-claude", "first", "operator", 1.0)
    store.set("master-account-claude-3", "third", "operator", 1.0)
    monkeypatch.setattr(place, "_client", lambda environ: None)
    monkeypatch.setattr(reservation, "_store", lambda environ: None)
    monkeypatch.setattr(reservation, "interactive_sessions", lambda harness, slug, proc=None: 0)
    monkeypatch.setattr(reservation, "master_agents", lambda store, harness, slug: int(harness == "claude"))
    return {"AGENTIHOOKS_HOME": str(tmp_path), "AH_CC_TOKEN_third": "three"}


def _result(name, week_used):
    return balancer.ProbeResult(
        name, "allowed", "NORMAL", 30.0, balancer.QuotaWindow(used=10.0), balancer.QuotaWindow(used=week_used)
    )


def _route(environ, tmp_path, monkeypatch, week_used, agent):
    monkeypatch.setattr(balancer, "collect_results", lambda *a, **k: ([_result("third", week_used)], "live"))
    decision = balancer.select_credential({**environ, **agent}, cache_file=tmp_path / "cache.json", sessions={}, now=0)
    assert decision.credential is not None
    return decision.credential.env_name


def _row(name, week_left):
    return capacity.Account("claude", name, "OPEN", 1, 80.0, week_left, 6)


@pytest.mark.feature("worker-ceiling-every-master-login-slot")
def test_a_worker_offer_on_a_slot_three_account_past_the_ceiling_is_refused(environ, tmp_path, monkeypatch):
    with pytest.raises(balancer.RoutingError):
        _route(environ, tmp_path, monkeypatch, 70.0, WORKER)
    assert _route(environ, tmp_path, monkeypatch, 70.0, MASTER) == "AH_CC_TOKEN_third"


@pytest.mark.feature("worker-ceiling-every-master-login-slot")
def test_a_worker_takes_a_slot_three_account_below_the_ceiling(environ, tmp_path, monkeypatch):
    assert _route(environ, tmp_path, monkeypatch, 69.9, WORKER) == "AH_CC_TOKEN_third"


@pytest.mark.feature("worker-ceiling-every-master-login-slot")
def test_capacity_closes_a_slot_three_row_past_the_ceiling_beside_slot_one(environ):
    rows = capacity.reserved_accounts([_row("first", 30.0), _row("third", 30.0), _row("spare", 30.0)], environ)
    assert [(row.name, row.cap, row.reserved) for row in rows] == [
        ("first", 0, REASON),
        ("third", 0, REASON),
        ("spare", 6, ""),
    ]
    rows = capacity.reserved_accounts([_row("first", 30.1), _row("third", 30.1)], environ)
    assert [(row.name, row.cap, row.reserved) for row in rows] == [("first", 6, ""), ("third", 6, "")]


@pytest.mark.feature("worker-ceiling-every-master-login-slot")
def test_slot_one_readers_keep_the_single_declared_account(environ):
    assert reservation.current("claude", environ) == reservation.Reservation("claude", "first", 1)
    assert reservation.for_worker("claude", {**environ, **WORKER}) == reservation.Reservation("claude", "first", 1)
    assert reservation.current("codex", environ) is None
