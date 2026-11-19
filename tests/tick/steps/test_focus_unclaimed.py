"""The focus unclaimed tick step counts the ticks a focused, claimable task stays unclaimed, and past three ticks
the master gets a focus unclaimed health finding."""

from importlib import import_module
from typing import Any

import pytest

from scripts.tick import registry
from tests.swarm.test_freeze import doc, record, task

SLUG = "sw"


@pytest.fixture
def store() -> Any:
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=1))
    return found


def ticks(store: Any, ledger: dict[str, Any], count: int) -> list[Any]:
    from scripts.swarm.health import focus_unclaimed

    for at in range(count):
        focus_unclaimed.observe(store, SLUG, ledger, 1000 + at)
    return focus_unclaimed.findings(store, SLUG)


@pytest.mark.feature("swarm-focus-unclaimed-finding")
def test_focus_unclaimed() -> None:
    """The focus unclaimed step registers in group wrapper phases on the tick lane with its feature and ports."""
    import_module("scripts.tick.steps.host")
    found = {step.name: step for step in registry.steps()}["focus unclaimed"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "wrapper phases",
        "tick",
        "swarm-focus-unclaimed-finding",
        frozenset({"coordination"}),
    )


@pytest.mark.feature("swarm-focus-unclaimed-finding")
def test_a_focused_claimable_task_unclaimed_past_three_ticks_is_a_finding(store: Any) -> None:
    ledger = doc(record("phases/p1", "focus"), tasks=[task("t1", title="Ship the fix"), task("t2", depends_on=["t9"])])
    assert ticks(store, ledger, 3) == []
    [found] = ticks(store, ledger, 1)
    assert (found.kind, found.subject, found.measure, found.threshold) == ("focus unclaimed", "t1", 4, "three ticks")
    assert found.summary == "focused task Ship the fix has been claimable but unclaimed for 4 ticks"
    assert found.evidence == ("the focus on phases/p1 admits it and no agent claimed it",)
    assert found.evidence_identity == (1000,)


@pytest.mark.feature("swarm-focus-unclaimed-finding")
def test_a_claim_or_a_hold_drops_the_count(store: Any) -> None:
    rows = [task("t1")]
    ledger = doc(record("tasks/t1", "focus"), tasks=rows)
    ticks(store, ledger, 3)
    rows[0]["state"] = "claimed"
    assert ticks(store, ledger, 1) == []
    rows[0]["state"] = "open"
    assert ticks(store, ledger, 3) == []
    waiting = doc(record("tasks/t1", "focus"), tasks=[{**rows[0], "depends_on": ["t9"]}, task("t9", phase="p2")])
    assert ticks(store, waiting, 5) == []


@pytest.mark.feature("swarm-focus-unclaimed-finding")
def test_no_focus_raises_no_finding(store: Any) -> None:
    assert ticks(store, doc(tasks=[task("t1")]), 5) == []


@pytest.mark.feature("swarm-focus-unclaimed-finding")
def test_the_finding_goes_to_the_master_inbox(store: Any) -> None:
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events
    from scripts.swarm.store import MASTER, AgentRecord

    lead = f"master@{SLUG}"
    store.put_agent(SLUG, AgentRecord("sw-master-1", MASTER, MASTER, pane_id="pm1", seat=lead))
    [found] = ticks(store, doc(record("tasks/t1", "focus"), tasks=[task("t1")]), 4)
    inbox = InboxStore(store.redis)
    shown = {"phases": [{"id": "p1", "title": "Build"}], "tasks": [], "_meta": {"rev": 1, "events": []}}
    ledger_events.findings_pass(inbox, store, SLUG, [{"id": found.id, **found.as_dict()}], shown)
    [item] = inbox.pending_items(lead)
    assert "(focus unclaimed)" in item.text
    assert "claimable but unclaimed for 4 ticks" in item.text
