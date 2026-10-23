from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus
from scripts.inbox.bus import Message, SeatInbox
from tests.inbox.test_wake import FakeHerdr, FakeLedger

pytestmark = pytest.mark.unit
W = 300_000


@pytest.fixture
def rig(monkeypatch):
    import fakeredis
    from scripts.hub.inbox_wake import InboxWake

    from scripts.hub.timers import Timers
    from scripts.inbox.store import InboxStore
    from scripts.swarm.store import AgentRecord

    clock = SimpleNamespace(now=1000)
    monkeypatch.setattr("scripts.inbox.store.now_ms", lambda: clock.now)
    inbox = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    worker = AgentRecord("worker", "eng", "task", pane_id="p1", harness="codex")
    master = AgentRecord("boss", "master", "master", seat="master@sw")
    store = SimpleNamespace(redis=inbox.redis, agents=lambda slug: [worker, master])
    bus, herdr, ledger = open_bus(BusConfig()), FakeHerdr({"p1": "idle"}), FakeLedger()
    reactor = InboxWake(bus, store=store, herdr=herdr, ledger=ledger, clock=lambda: clock.now)
    return SimpleNamespace(
        bus=bus,
        inbox=inbox,
        clock=clock,
        herdr=herdr,
        ledger=ledger,
        reactor=reactor,
        timers=Timers(bus, lambda: clock.now),
        reader=bus.events.consumer("proof", "swarm.*.>"),
    )


def pump(rig):
    for _ in range(50):
        deliveries = rig.reader.fetch(100, 0)
        if not deliveries:
            return
        for delivery in deliveries:
            event = delivery.event
            rig.reactor.handle(event)
            if event.type in {"deadline.scheduled", "deadline.cancelled"}:
                rig.timers.handle(event)
            delivery.ack()
    pytest.fail("inbox timer events did not settle")


def send(rig, address="worker"):
    item = rig.inbox.send("sender", address, "read this request")
    event = SeatInbox(rig.bus, "sw").send(address, Message(item.id, item.sender, item.text))
    pump(rig)
    return item, event


def advance(rig, at):
    rig.clock.now = at
    rig.timers.fire()
    pump(rig)


def steps(rig, item):
    return [row["event"] for row in rig.inbox.history(item.id) if "event" in row]


def test_timer_retries_three_wakes_then_escalates_to_master_and_operator_without_tick(rig):
    item, _ = send(rig)
    advance(rig, 1000)
    assert steps(rig, item) == ["woken"]
    advance(rig, 1000 + W - 1)
    assert len(rig.herdr.prompts) == 1
    advance(rig, 1000 + W)
    advance(rig, 1000 + 2 * W)
    assert len(rig.herdr.prompts) == 3
    advance(rig, 1000 + 3 * W)
    assert steps(rig, item) == ["woken", "woken", "woken", "escalated_master"]
    assert len(rig.inbox.inbox("master@sw")) == 1
    assert rig.ledger.followups == []
    advance(rig, 1000 + 4 * W)
    assert steps(rig, item)[-1] == "escalated_operator"
    assert rig.ledger.flagged == [True]


def test_busy_pane_escalates_on_timer_and_never_receives_typed_input(rig):
    rig.herdr.status["p1"] = "working"
    item, _ = send(rig)
    advance(rig, 1000)
    advance(rig, 1000 + W)
    assert rig.herdr.prompts == []
    assert steps(rig, item) == ["escalated_master"]


def test_master_mail_uses_timer_to_escalate_straight_to_operator(rig):
    item, _ = send(rig, "boss")
    advance(rig, 1000 + W)
    assert rig.herdr.prompts == []
    assert steps(rig, item) == ["escalated_operator"]
    assert rig.ledger.flagged == [True]


def test_timer_restart_and_duplicate_arrival_do_not_repeat_wakes(rig):
    from scripts.hub.inbox_wake import InboxWake

    from scripts.hub.timers import Timers

    item, event = send(rig)
    advance(rig, 1000)
    rig.reactor = InboxWake(
        rig.bus, store=rig.reactor.store, herdr=rig.herdr, ledger=rig.ledger, clock=lambda: rig.clock.now
    )
    rig.timers = Timers(rig.bus, lambda: rig.clock.now)
    rig.reactor.handle(event)
    advance(rig, 1000)
    assert steps(rig, item) == ["woken"]
    advance(rig, 1000 + W)
    assert steps(rig, item) == ["woken", "woken"]


def test_closed_item_and_unrelated_deadline_never_advance_ladder(rig):
    item, event = send(rig)
    rig.inbox.close(item.id, "worker", "done", "handled")
    advance(rig, 1000 + W)
    rig.reactor.handle(replace(event, type="execution.changed"))
    assert rig.herdr.prompts == []
    assert rig.ledger.followups == []
    assert steps(rig, item) == []
