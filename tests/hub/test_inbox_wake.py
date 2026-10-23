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
    monkeypatch.setattr("scripts.inbox.bus.time.time_ns", lambda: clock.now * 1_000_000)
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


def test_hive_mail_is_resolved_from_its_own_world(rig, monkeypatch):
    from scripts.hive import scope
    from scripts.inbox.store import InboxStore

    monkeypatch.setattr(scope, "member", lambda: True)
    member = InboxStore(rig.inbox.redis, world="hive")
    item = member.send("sender", "worker", "hive request")
    monkeypatch.setattr(scope, "member", lambda: False)
    monkeypatch.setattr(scope, "members", lambda redis: ["hive"])
    SeatInbox(rig.bus, "sw").send(item.address, Message(item.id, item.sender, item.text))
    pump(rig)
    advance(rig, 1000)
    assert [row.get("event") for row in member.history(item.id) if "event" in row] == ["woken"]
    assert rig.herdr.prompts == [
        (
            "p1",
            "Run agentihooks msg inbox and answer through agentihooks msg reply or the swarm commands, never as text in the terminal.",
        )
    ]


def test_unconfirmed_delivery_requeues_and_keeps_timer_retries_without_tick(rig):
    item, _ = send(rig)
    rig.inbox.deliver(item.id, "worker")
    advance(rig, 1000)
    assert rig.herdr.prompts == []
    advance(rig, 1000 + W)
    assert rig.inbox.get(item.id).state == "pending"
    assert steps(rig, item) == ["woken"]


def test_persisted_next_deadline_is_republished_after_publish_failure(rig, monkeypatch):
    from scripts.events import deadlines
    from scripts.events.envelope import Envelope

    item, _ = send(rig)
    requests = rig.bus.events.consumer("requests-proof", "swarm.*.deadline.scheduled").fetch(10, 0)
    expired = deadlines.expired(requests[0].event)
    publish = rig.bus.events.publish

    def fail(subject, event):
        raise ConnectionError("bus disconnected")

    monkeypatch.setattr(rig.bus.events, "publish", fail)
    with pytest.raises(ConnectionError, match="bus disconnected"):
        rig.reactor.handle(expired)
    monkeypatch.setattr(rig.bus.events, "publish", publish)
    rig.reactor.handle(expired)
    saved = Envelope.decode(rig.reactor.state.get(rig.reactor._key("sw", item.id)).value)
    assert saved.version == 2
    assert saved.data["deadline_ms"] == 1000 + W
    pump(rig)
    advance(rig, 1000 + W)
    assert steps(rig, item) == ["woken", "woken"]


def test_failed_arrival_publication_stays_in_the_durable_outbox(rig, monkeypatch):
    from scripts.swarm.naming import NameRegistry

    monkeypatch.setattr(NameRegistry, "slug_of", lambda self, name: "sw")
    item = rig.inbox.send("sender", "worker", "request during bus outage")
    publish = rig.bus.events.publish
    monkeypatch.setattr(rig.bus.events, "publish", lambda *args: (_ for _ in ()).throw(ConnectionError("disconnected")))
    with pytest.raises(ConnectionError, match="disconnected"):
        rig.reactor.flush()
    assert rig.inbox.redis.smembers(rig.inbox.key("wake-outbox")) == {item.id}
    monkeypatch.setattr(rig.bus.events, "publish", publish)
    rig.reactor.flush()
    assert rig.inbox.redis.smembers(rig.inbox.key("wake-outbox")) == set()
    pump(rig)
    advance(rig, 1000)
    assert steps(rig, item) == ["woken"]


def test_suppressed_escalation_reference_has_no_millisecond_timer_loop(rig):
    rig.herdr.status["p1"] = "working"
    item = rig.inbox.send("sender", "worker", "request", ref="inbox-escalation:original")
    SeatInbox(rig.bus, "sw").send(item.address, Message(item.id, item.sender, item.text, item.ref))
    pump(rig)
    advance(rig, 1000 + W)
    advance(rig, 1000 + W + 1)
    assert steps(rig, item) == []
    requests = rig.bus.events.consumer("bounded-proof", "swarm.*.deadline.scheduled").fetch(10, 0)
    assert [row.event.data["deadline_ms"] for row in requests] == [1000, 1000 + 2 * W]


def test_production_reactors_drive_wakes_through_hub_delivery(rig):
    from scripts.hub.registry import Registry
    from scripts.hub.runner import HubRunner

    item, _ = send(rig)
    hub = HubRunner(
        rig.bus,
        Registry([rig.reactor.reactor(), rig.reactor.expiry_reactor(), rig.timers.reactor()]),
        "hub-proof",
        clock=lambda: rig.clock.now,
    )
    for _ in range(6):
        hub.run_once(timeout_ms=0)
    assert steps(rig, item) == ["woken"]
    rig.clock.now += W
    for _ in range(6):
        hub.run_once(timeout_ms=0)
    assert steps(rig, item) == ["woken", "woken"]
