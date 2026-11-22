"""The order waiting tick step notes each open operator order nobody works on, and past thirty minutes the master
gets an order waiting health finding."""

from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.tick import registry
from scripts.tick.steps import hook_domains
from tests.swarm.test_freeze import record, task
from tests.swarm.test_order_check import ORDER, ledger_doc

SLUG = "sw"
MINUTE = 60_000
START = 1_800_000_000_000
UNCLAIMED = "every open task under it is claimable and no agent claimed one"


@pytest.fixture
def store() -> Any:
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=1))
    return found


def watched(store: Any, ledger: dict[str, Any], minutes: int, start: int = START) -> list[Any]:
    """The findings once the step saw the ledger at the start and again the given minutes later."""
    from scripts.swarm.health import order_waiting

    order_waiting.observe(store, SLUG, ledger, start)
    order_waiting.observe(store, SLUG, ledger, start + minutes * MINUTE)
    return order_waiting.findings(store, SLUG)


@pytest.mark.feature("swarm-order-waiting-finding")
def test_order_waiting() -> None:
    """The order waiting step registers in group wrapper phases on the slow lane with its feature and ports."""
    import_module("scripts.tick.steps.orders")
    found = {step.name: step for step in registry.steps()}["order waiting"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "wrapper phases",
        "slow",
        "swarm-order-waiting-finding",
        frozenset({"coordination"}),
    )
    assert (found.reads, found.interval_s) == (hook_domains("doc"), 600)


@pytest.mark.feature("swarm-order-waiting-finding")
def test_an_order_with_open_tasks_and_nothing_in_flight_past_thirty_minutes_is_a_finding(store: Any) -> None:
    tasks = [
        task("t1"),
        task("t2", depends_on=["t9"]),
        task("t3", depends_on=["t9"]),
        task("t4"),
        task("t5", state="done"),
        task("t9", phase="p2"),
    ]
    ledger = ledger_doc(tasks, record("tasks/t4"), p1=ORDER)
    assert watched(store, ledger, 30) == []
    [found] = watched(store, ledger, 31)
    assert (found.kind, found.subject, found.measure, found.threshold) == (
        "order waiting",
        "phases/p1",
        31,
        "thirty minutes",
    )
    assert found.summary == (
        "operator order phases/p1 Build: 4 unfinished, 1 claimable, none claimed or in a pull request for 31 minutes"
    )
    assert found.evidence == ("it waits on task t9", "the freeze on task t4 holds it")
    assert found.evidence_identity == (START,)


@pytest.mark.feature("swarm-order-waiting-finding")
def test_an_order_whose_open_tasks_are_all_claimable_says_nobody_claimed_one(store: Any) -> None:
    [found] = watched(store, ledger_doc([task("t1"), task("t2")], p1=ORDER), 45)
    assert found.summary == (
        "operator order phases/p1 Build: 2 unfinished, 2 claimable, none claimed or in a pull request for 45 minutes"
    )
    assert found.evidence == (UNCLAIMED,)


@pytest.mark.feature("swarm-order-waiting-finding")
@pytest.mark.parametrize("busy", [{"state": "claimed", "claimed_by": "engineer@one"}, {"state": "pr"}])
def test_work_in_flight_under_an_order_drops_it_and_the_wait_starts_again(store: Any, busy: dict[str, str]) -> None:
    from scripts.swarm.health import order_waiting

    rows = [task("t1"), task("t2")]
    ledger = ledger_doc(rows, p1=ORDER)
    assert len(watched(store, ledger, 31)) == 1
    rows[0].update(busy)
    order_waiting.observe(store, SLUG, ledger, START + 32 * MINUTE)
    assert order_waiting.findings(store, SLUG) == []
    assert order_waiting.read(store, SLUG) == {"at": START + 32 * MINUTE, "orders": {}}
    rows[0].update(state="open", claimed_by="")
    assert watched(store, ledger, 30, START + 40 * MINUTE) == []
    [found] = watched(store, ledger, 31, START + 40 * MINUTE)
    assert found.evidence_identity == (START + 40 * MINUTE,)


@pytest.mark.feature("swarm-order-waiting-finding")
def test_an_order_with_no_open_task_a_done_order_and_a_ledger_without_orders_raise_none(store: Any) -> None:
    assert watched(store, ledger_doc([task("t5", state="done")], p1=ORDER), 90) == []
    assert watched(store, ledger_doc([task("t2", out_of_scope=True)], p1=ORDER), 90) == []
    assert watched(store, ledger_doc([task("t1", state="done", ordered=ORDER)]), 90) == []
    assert watched(store, ledger_doc([task("t1")]), 90) == []


@pytest.mark.feature("swarm-order-waiting-finding")
def test_each_pass_stores_its_time_and_keeps_the_time_an_order_was_first_seen_waiting(store: Any) -> None:
    from scripts.swarm.health import order_waiting

    ledger = ledger_doc([task("t1"), task("t2", depends_on=["t9"]), task("t9", phase="p2")], p1=ORDER)
    assert order_waiting.read(store, SLUG) == {}
    assert order_waiting.findings(store, SLUG) == []
    order_waiting.observe(store, SLUG, ledger, START)
    noted = order_waiting.observe(store, SLUG, ledger, START + MINUTE)
    waiting = {"since": START, "title": "Build", "unfinished": 2, "claimable": 1, "held": ["it waits on task t9"]}
    assert noted == {"phases/p1": waiting}
    assert order_waiting.read(store, SLUG) == {"at": START + MINUTE, "orders": {"phases/p1": waiting}}
    assert store.redis.get(store.key(SLUG, "order-waiting")) is not None


@pytest.mark.feature("swarm-order-waiting-finding")
def test_an_ordered_task_and_an_ordered_phase_are_each_one_finding(store: Any) -> None:
    ledger = ledger_doc([task("t1", title="Seats"), task("t7", phase="p2", title="Rooms", ordered=ORDER)], p1=ORDER)
    found = watched(store, ledger, 40)
    assert [(one.subject, one.summary) for one in found] == [
        (
            "phases/p1",
            "operator order phases/p1 Build: 1 unfinished, 1 claimable, none claimed or in a pull request for 40 minutes",
        ),
        (
            "tasks/t7",
            "operator order tasks/t7 Rooms: 1 unfinished, 1 claimable, none claimed or in a pull request for 40 minutes",
        ),
    ]


@pytest.mark.feature("swarm-order-waiting-finding")
def test_the_step_notes_the_orders_of_the_pass_state(store: Any) -> None:
    from scripts.swarm.health import order_waiting
    from scripts.tick.steps import orders

    state = {"store": store, "slug": SLUG, "doc": ledger_doc([task("t1")], p1=ORDER), "box": {}}
    assert orders._order_waiting(SimpleNamespace(now_ms=lambda: START), state.__getitem__) == []
    assert order_waiting.read(store, SLUG)["at"] == START
    assert list(order_waiting.read(store, SLUG)["orders"]) == ["phases/p1"]
    registered: Any = {step.name: step for step in registry.steps()}["order waiting"]
    assert registered.call(None, state.__getitem__) == []
    assert state["box"] == {}
    assert order_waiting.read(store, SLUG)["at"] != START
    assert order_waiting.read(store, SLUG)["orders"]["phases/p1"]["since"] == START


@pytest.mark.feature("swarm-order-waiting-finding")
def test_the_finding_is_in_the_swarm_status_findings_and_goes_to_the_master_inbox(store: Any) -> None:
    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events, status
    from scripts.swarm.health import order_waiting
    from scripts.swarm.store import MASTER, AgentRecord

    lead = f"master@{SLUG}"
    store.put_agent(SLUG, AgentRecord("sw-master-1", MASTER, MASTER, pane_id="pm1", seat=lead))
    [found] = watched(store, ledger_doc([task("t1")], p1=ORDER), 31)
    assert found.id == "order-waiting/phases/p1"
    assert order_waiting.findings(store, SLUG) == [found]
    shown = status.findings(store, SLUG, store.config(SLUG), [], [])
    assert [(row["subject"], row["summary"]) for row in shown if row["kind"] == "order waiting"] == [
        (found.subject, found.summary)
    ]
    inbox = InboxStore(store.redis)
    shown = {"phases": [{"id": "p1", "title": "Build"}], "tasks": [], "_meta": {"rev": 1, "events": []}}
    ledger_events.findings_pass(inbox, store, SLUG, [{"id": found.id, **found.as_dict()}], shown)
    [item] = inbox.pending_items(lead)
    assert "(order waiting)" in item.text
    assert "none claimed or in a pull request for 31 minutes" in item.text
