from types import SimpleNamespace
from typing import cast

import pytest

from scripts.events import memory
from scripts.events.bus import Bus
from scripts.events.config import BusConfig
from scripts.events.envelope import Envelope
from scripts.hub import registry, wait_ends
from scripts.inbox.store import InboxStore
from scripts.swarm import idle
from scripts.swarm.store import AgentRecord, RedisStore

ME = "engineer@a1b2c3-0001"
PEER = "engineer@a1b2c3-0002"
ON_TASK = {"kind": "task", "target": "t9"}


def _ended(outcome: str) -> str:
    return (
        f"Your wait on {outcome} has ended. Pick task t1 back up: agentihooks swarm sw done, block, "
        "or wait on the next thing."
    )


def _task_event(task: str = "t9", state: str = "done") -> Envelope:
    return Envelope(
        f"e-{task}-{state}", "task.changed", "sw", "task", task, 1, 5_000, "ledger", {"id": task, "state": state}
    )


def _wake(item_id: str) -> Envelope:
    return Envelope(f"wake-{item_id}", "inbox.wake", "sw", "inbox-wake", item_id, 1, 5_000, "swarm", {"address": ME})


@pytest.fixture
def rig() -> SimpleNamespace:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.put_agent("sw", AgentRecord(name=ME, lane="eng", task="t1", seat="eng-1@sw"))
    store.put_agent("sw", AgentRecord(name=PEER, lane="eng", task="t2", seat="eng-2@sw"))
    rows = {
        "t1": {"id": "t1", "state": "claimed", "claimed_by": ME},
        "t2": {"id": "t2", "state": "claimed", "claimed_by": PEER},
        "t9": {"id": "t9", "state": "done"},
    }
    reads: list[str] = []

    def task(slug: str, task_id: str) -> dict | None:
        reads.append(task_id)
        return rows.get(task_id)

    react = wait_ends.WaitEnds(store, SimpleNamespace(task=task), lambda url: None, clock=lambda: 5_000)

    def hold(held: dict) -> None:
        idle.declare_wait(store.redis, "sw", ME, 10_000_000, "", 1, on=held)

    def told(seat: str = "eng-1@sw") -> list[str]:
        return [item.text for item in InboxStore(store.redis).inbox(seat) if item.sender == "swarm"]

    return SimpleNamespace(store=store, rows=rows, reads=reads, react=react, hold=hold, told=told)


@pytest.mark.parametrize("state", ["done", "blocked"])
@pytest.mark.feature("wait-ends-on-task-event")
def test_a_task_event_to_done_or_blocked_ends_a_task_wait_on_it_with_one_item_and_the_polled_records(
    rig: SimpleNamespace, state: str
) -> None:
    rig.rows["t9"]["state"] = state
    rig.hold(ON_TASK)
    rig.react.task_changed(_task_event(state=state))
    assert rig.told() == [_ended(f"task t9, now {state}")]
    assert idle.wait(rig.store.redis, "sw", ME) is None
    assert idle.woke(rig.store.redis, "sw", ME) == idle.waited(rig.store.redis, "sw", ME) == 5_000
    assert idle.wait_history(rig.store.redis, "sw", ME) == [{"until": 5_000, "reason": "", "at": 1, "on": ON_TASK}]


@pytest.mark.feature("wait-ends-on-task-event")
def test_a_done_event_for_another_task_leaves_the_task_wait_open(rig: SimpleNamespace) -> None:
    rig.rows["t8"] = {"id": "t8", "state": "done"}
    rig.hold(ON_TASK)
    rig.react.task_changed(_task_event("t8"))
    assert idle.wait(rig.store.redis, "sw", ME) == {"until": 10_000_000, "reason": "", "at": 1, "on": ON_TASK}
    assert rig.told() == []


@pytest.mark.feature("wait-ends-on-task-event")
def test_a_task_event_to_a_state_that_ends_nothing_leaves_the_wait_open_and_reads_nothing(
    rig: SimpleNamespace,
) -> None:
    rig.hold(ON_TASK)
    rig.react.task_changed(_task_event(state="claimed"))
    assert idle.wait(rig.store.redis, "sw", ME) == {"until": 10_000_000, "reason": "", "at": 1, "on": ON_TASK}
    assert rig.told() == []
    assert rig.reads == []


@pytest.mark.feature("wait-ends-on-task-event")
def test_a_task_event_the_fresh_ledger_row_has_not_closed_leaves_the_wait_open(rig: SimpleNamespace) -> None:
    rig.rows["t9"]["state"] = "claimed"
    rig.hold(ON_TASK)
    rig.react.task_changed(_task_event())
    assert idle.wait(rig.store.redis, "sw", ME) == {"until": 10_000_000, "reason": "", "at": 1, "on": ON_TASK}
    assert rig.told() == []


@pytest.mark.feature("wait-ends-on-task-event")
def test_a_reply_ends_a_reply_wait_on_the_answered_item_with_one_item(rig: SimpleNamespace) -> None:
    inbox = InboxStore(rig.store.redis)
    asked = inbox.send(ME, "helper", "which branch?")
    rig.hold({"kind": "reply", "target": asked.id})
    answer = inbox.reply(asked.id, "helper", "the dev branch")
    rig.react.replied(_wake(answer.id))
    assert rig.told() == [_ended(f"message {asked.id}, now done")]
    assert idle.wait(rig.store.redis, "sw", ME) is None


@pytest.mark.feature("wait-ends-on-task-event")
def test_a_reply_wake_before_its_answered_item_closes_is_redelivered_until_it_closes(rig: SimpleNamespace) -> None:
    inbox = InboxStore(rig.store.redis)
    asked = inbox.send(ME, "helper", "which branch?")
    held = {"kind": "reply", "target": asked.id}
    rig.hold(held)
    answer = inbox.send("helper", ME, "the dev branch", ref=asked.id)
    with pytest.raises(RuntimeError, match=asked.id):
        rig.react.replied(_wake(answer.id))
    assert idle.wait(rig.store.redis, "sw", ME) == {"until": 10_000_000, "reason": "", "at": 1, "on": held}
    inbox.close(asked.id, "helper", "done", "replied")
    rig.react.replied(_wake(answer.id))
    assert rig.told() == [_ended(f"message {asked.id}, now done")]


@pytest.mark.feature("wait-ends-on-task-event")
def test_a_send_that_answers_nothing_leaves_the_reply_wait_open(rig: SimpleNamespace) -> None:
    inbox = InboxStore(rig.store.redis)
    asked = inbox.send(ME, "helper", "which branch?")
    held = {"kind": "reply", "target": asked.id}
    rig.hold(held)
    other = inbox.send("helper", ME, "unrelated news")
    rig.react.replied(_wake(other.id))
    assert idle.wait(rig.store.redis, "sw", ME) == {"until": 10_000_000, "reason": "", "at": 1, "on": held}
    assert rig.told() == []


@pytest.mark.feature("wait-ends-on-task-event")
def test_the_hub_registry_wires_the_task_and_reply_wait_end_reactors_as_singletons() -> None:
    bus = cast(Bus, memory.open_bus(BusConfig()))
    found = {reactor.name: reactor for reactor in registry.configured(bus)}
    assert found["task-wait-ends"].subject == "swarm.*.task.*"
    assert found["reply-wait-ends"].subject == "swarm.*.inbox-wake.requested"
    assert found["task-wait-ends"].singleton and found["reply-wait-ends"].singleton
    bus.close()


@pytest.mark.feature("dependency-closed-notice")
def test_the_live_holder_of_a_claimed_dependent_hears_once_when_its_dependency_is_done(rig: SimpleNamespace) -> None:
    rig.rows["t2"]["depends_on"] = ["t9"]
    rig.react.task_changed(_task_event())
    rig.react.task_changed(_task_event())
    assert rig.told("eng-2@sw") == ["Task t9, which your task t2 depends on, has closed as done."]
    assert rig.told() == []


@pytest.mark.parametrize(
    "row, state",
    [
        ({"id": "t2", "state": "open", "claimed_by": "", "depends_on": ["t9"]}, "working"),
        ({"id": "t2", "state": "claimed", "claimed_by": PEER, "depends_on": ["t9"]}, "finished"),
        ({"id": "t2", "state": "done", "claimed_by": PEER, "depends_on": ["t9"]}, "working"),
        ({"id": "t2", "state": "claimed", "claimed_by": PEER, "depends_on": ["t7"]}, "working"),
    ],
    ids=["unclaimed-dependent", "finished-holder", "closed-dependent", "other-dependency"],
)
@pytest.mark.feature("dependency-closed-notice")
def test_a_done_dependency_tells_no_holder_that_is_not_live_on_a_claimed_open_dependent(
    rig: SimpleNamespace, row: dict, state: str
) -> None:
    rig.rows["t2"] = row
    rig.store.put_agent("sw", AgentRecord(name=PEER, lane="eng", task="t2", seat="eng-2@sw", state=state))
    rig.react.task_changed(_task_event())
    assert rig.told("eng-2@sw") == []


@pytest.mark.feature("dependency-closed-notice")
def test_a_blocked_dependency_tells_no_dependent(rig: SimpleNamespace) -> None:
    rig.rows["t9"]["state"] = "blocked"
    rig.rows["t2"]["depends_on"] = ["t9"]
    rig.react.task_changed(_task_event(state="blocked"))
    assert rig.told("eng-2@sw") == []
