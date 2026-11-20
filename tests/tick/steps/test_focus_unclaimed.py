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


@pytest.mark.feature("swarm-focus-unclaimed-finding")
def test_each_tick_stores_its_time_and_keeps_the_first_unclaimed_time(store: Any) -> None:
    from scripts.swarm.health import focus_unclaimed

    ledger = doc(record("tasks/t1", "focus"), tasks=[task("t1")])
    focus_unclaimed.observe(store, SLUG, ledger, 1000)
    focus_unclaimed.observe(store, SLUG, ledger, 2000)
    stored = focus_unclaimed.read(store, SLUG)
    assert stored["at"] == 2000
    assert stored["tasks"] == {"t1": {"ticks": 2, "since": 1000, "title": "t1", "focus": "tasks/t1"}}


@pytest.mark.feature("swarm-focus-unclaimed-finding")
def test_a_freeze_record_before_the_focus_does_not_stop_the_count(store: Any) -> None:
    ledger = doc(record("phases/p2"), record("tasks/t1", "focus"), tasks=[task("t1")])
    assert [found.subject for found in ticks(store, ledger, 4)] == ["t1"]


@pytest.mark.feature("swarm-focus-unclaimed-finding")
def test_a_task_the_gate_holds_does_not_stop_the_count_of_the_next_one(store: Any) -> None:
    ledger = doc(record("phases/p1", "focus"), tasks=[task("t1", state="claimed"), task("t2")])
    assert [found.subject for found in ticks(store, ledger, 4)] == ["t2"]


@pytest.mark.feature("swarm-focus-unclaimed-finding")
def test_a_task_below_three_ticks_does_not_stop_the_finding_of_the_next_one(store: Any) -> None:
    rows = [task("t1", state="claimed"), task("t2")]
    ledger = doc(record("phases/p1", "focus"), tasks=rows)
    ticks(store, ledger, 3)
    rows[0]["state"] = "open"
    assert [found.subject for found in ticks(store, ledger, 1)] == ["t2"]


@pytest.mark.feature("swarm-focus-unclaimed-finding")
def test_no_stored_count_is_no_finding(store: Any) -> None:
    from scripts.swarm.health import focus_unclaimed

    assert focus_unclaimed.read(store, SLUG) == {}
    assert focus_unclaimed.findings(store, SLUG) == []


@pytest.mark.feature("swarm-focus-unclaimed-finding")
def test_the_doctor_fix_phase_comes_from_the_swarm_config(store: Any) -> None:
    from scripts.doctor import loop, priming
    from scripts.swarm import freeze
    from scripts.swarm.health import focus_unclaimed
    from scripts.swarm.store import SwarmConfig

    store.create(SwarmConfig("fix", "/repo", max_eng=1, max_ci=1, template=priming.TEMPLATE))
    rows = [task("t1", phase=loop.FIX_PHASE)]
    ledger = {**doc(record("lane:eng", "focus"), tasks=rows), freeze.WATCHED: ["the focus elsewhere"]}
    assert ticks(store, ledger, 4) == []
    for at in range(4):
        focus_unclaimed.observe(store, "fix", ledger, at)
    assert [found.subject for found in focus_unclaimed.findings(store, "fix")] == ["t1"]
