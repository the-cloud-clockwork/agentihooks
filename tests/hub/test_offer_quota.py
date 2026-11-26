from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.swarm import capacity, offer_quota, runtime
from scripts.swarm.offer_quota import accounts as real_accounts
from tests.hub.test_admission import SLUG, ask
from tests.hub.test_admission import rig as rig


@pytest.fixture(autouse=True)
def _seats_read_through_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(offer_quota, "accounts", real_accounts)


def _accounts(monkeypatch: pytest.MonkeyPatch, rows: list[capacity.Account]) -> Mock:
    reader = Mock(side_effect=lambda *_args, **_kwargs: list(rows))
    monkeypatch.setattr(capacity, "accounts", reader)
    monkeypatch.setattr(runtime.plugins, "claude_only", lambda profile: profile == "claude-only")
    return reader


@pytest.mark.feature("event-offer-account-seats")
@pytest.mark.parametrize("codex_sessions", [0, 6])
@pytest.mark.parametrize("requirement", ["harness", "profile", "lane"])
def test_a_claude_offer_is_held_before_claim_until_its_account_has_a_seat(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, codex_sessions: int, requirement: str
) -> None:
    rows = [
        capacity.Account("claude", "claude-a", "OPEN", 3, None, 90, 3),
        capacity.Account("codex", "codex-a", "OPEN", codex_sessions, None, 90, 6),
    ]
    reader = _accounts(monkeypatch, rows)
    rig.rows["t1"].update(profile="planner", requires_harness="claude")
    if requirement == "profile":
        rig.rows["t1"].update(profile="claude-only", requires_harness="")
    if requirement == "lane":
        rig.rows["t1"]["requires_harness"] = ""
        rig.store.update(SLUG, lanes={"eng": {"agent": "claude"}})
    task = deepcopy(rig.rows["t1"])
    generation = rig.store.claim_generation(SLUG, "t1")
    lives = rig.store.claims(SLUG, "t1")

    for _ in range(4):
        held = rig.admitter.admit(SLUG, ask())
        assert held is not None and "held" in held
        assert rig.rows["t1"] == task
        assert rig.store.claimant(SLUG, "t1") is None
        assert rig.store.claim_generation(SLUG, "t1") == generation
        assert rig.store.claims(SLUG, "t1") == lives
        assert rig.store.agents(SLUG) == []
        assert rig.store.execution_occupants(SLUG) == {}
        assert rig.ledger.writes == []

    rows[0] = capacity.Account("claude", "claude-a", "OPEN", 2, None, 90, 3)
    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is not None and "held" not in admitted
    assert rig.store.claimant(SLUG, "t1") == admitted["name"]
    assert admitted["task"]["profile"] == task["profile"]
    assert admitted["task"]["requires_harness"] == task["requires_harness"]
    assert rig.store.claim_generation(SLUG, "t1") == generation + 1
    assert rig.store.claims(SLUG, "t1") == lives
    assert reader.call_args.kwargs["redis"] is rig.store.redis
    assert reader.call_args.kwargs["refresh"] is False


@pytest.mark.feature("event-offer-account-seats")
def test_a_mixed_offer_waits_for_either_harness_without_spending_a_claim_life(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        capacity.Account("claude", "claude-a", "OPEN", 3, None, 90, 3),
        capacity.Account("codex", "codex-a", "OPEN", 6, None, 90, 6),
    ]
    _accounts(monkeypatch, rows)
    rig.rows["t1"].update(profile="engineer", requires_harness="")
    before = deepcopy(rig.rows["t1"])

    assert "held" in rig.admitter.admit(SLUG, ask())
    assert rig.rows["t1"] == before
    assert rig.store.claim_generation(SLUG, "t1") == 0
    assert rig.store.claims(SLUG, "t1") == 0
    assert rig.store.agents(SLUG) == []
    assert rig.ledger.writes == []

    rows[1] = capacity.Account("codex", "codex-a", "OPEN", 5, None, 90, 6)
    admitted = rig.admitter.admit(SLUG, ask())

    assert admitted is not None and "held" not in admitted
    assert admitted["task"]["profile"] == "engineer"
    assert admitted["task"]["requires_harness"] == ""
    assert rig.store.claimant(SLUG, "t1") == admitted["name"]
    assert rig.store.claims(SLUG, "t1") == 0
