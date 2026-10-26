import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.events.config import BusConfig
from scripts.events.memory import open_bus
from scripts.inbox import wake
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
            wake.WAKE_TEXT,
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


def test_outage_time_is_excluded_before_timer_retry(rig):
    from scripts.swarm import outage

    item, _ = send(rig)
    advance(rig, 1000)
    outage.observe("sw", "provider", ["worker (codex): stream disconnected before completion"], 1100)
    advance(rig, 1000 + W)
    assert steps(rig, item) == ["woken"]
    outage.observe("sw", "provider", [], 1100 + W)
    advance(rig, 1000 + 2 * W - 1)
    assert steps(rig, item) == ["woken"]
    advance(rig, 1000 + 2 * W)
    assert steps(rig, item) == ["woken", "woken"]


def test_unoccupied_seat_waits_for_binding_before_wake_and_escalation(rig):
    item, _ = send(rig, "eng-1@sw")
    advance(rig, 1000)
    advance(rig, 1000 + W)
    assert steps(rig, item) == []
    rig.inbox.seats.occupy("eng-1@sw", "worker", at=1000 + W)
    advance(rig, 1000 + 2 * W - 1)
    assert steps(rig, item) == []
    advance(rig, 1000 + 2 * W)
    assert steps(rig, item) == ["woken"]


def test_stale_expiry_and_duplicate_arrival_preserve_the_next_timer(rig):
    from scripts.events import deadlines

    item, _ = send(rig)
    [arrival] = rig.bus.events.consumer("arrival-proof", "swarm.*.inbox-wake.requested").fetch(10, 0)
    [request] = rig.bus.events.consumer("request-proof", "swarm.*.deadline.scheduled").fetch(10, 0)
    advance(rig, 1000)
    rig.reactor.handle(arrival.event)
    rig.reactor.handle(deadlines.expired(request.event))
    rig.reactor.handle(replace(deadlines.expired(request.event), version=99))
    pump(rig)
    assert steps(rig, item) == ["woken"]
    advance(rig, 1000 + W)
    assert steps(rig, item) == ["woken", "woken"]


def test_pooled_master_arrival_is_recovered_by_the_outbox_and_escalates(rig):
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(rig.inbox.redis)
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1))
    masters = [
        AgentRecord("sw-master-1", "master", "master", seat="master@sw"),
        AgentRecord("sw-master-2", "master", "master", seat="master-2@sw"),
    ]
    for master in masters:
        store.put_agent("sw", master)
        store.seats.occupy(master.seat, master.name, at=1000)
    rig.reactor.store = store
    item = rig.inbox.send("worker", "master@sw", "request to the pool")
    rig.reactor.flush()
    pump(rig)
    advance(rig, 1000 + W)
    assert steps(rig, item) == ["escalated_operator"]
    assert rig.ledger.flagged == [True]


def test_configured_hub_routes_arrivals_and_expiry_using_its_clock(rig, monkeypatch):
    from scripts.hub import inbox_wake, registry
    from scripts.hub.runner import HubRunner

    monkeypatch.setattr(inbox_wake, "connect", lambda: rig.reactor.store)
    monkeypatch.setattr("scripts.inbox.bus.time.time_ns", lambda: 999_000_000)
    monkeypatch.setattr(inbox_wake, "HerdrMessenger", lambda: rig.herdr)
    monkeypatch.setattr(inbox_wake, "LedgerClient", lambda service: rig.ledger)
    reactors = registry.configured(rig.bus, clock=lambda: rig.clock.now)
    selected = [reactor for reactor in reactors if reactor.name in {"inbox-wake", "inbox-wake-expiry", "timers"}]
    assert len(selected) == 3
    assert all(reactor.singleton is True for reactor in selected)
    hub = HubRunner(rig.bus, registry.Registry(selected), "hub-one", clock=lambda: rig.clock.now)
    standby = HubRunner(rig.bus, registry.Registry(selected), "hub-two", clock=lambda: rig.clock.now)
    rig.inbox.seats.occupy("eng-1@sw", "worker", at=1000)
    item = rig.inbox.send("sender", "eng-1@sw", "wake through configured hub")
    for _ in range(6):
        hub.run_once(timeout_ms=0)
    assert steps(rig, item) == ["woken"]
    assert standby.run_once(timeout_ms=0) == {
        "timers": ["standby"],
        "inbox-wake": ["standby"],
        "inbox-wake-expiry": ["standby"],
    }
    rig.clock.now += W
    for _ in range(6):
        hub.run_once(timeout_ms=0)
    assert steps(rig, item) == ["woken", "woken"]


def test_default_hub_ledger_uses_service_identity_and_stable_timer_namespace(rig):
    from scripts.hub.inbox_wake import InboxWake

    default = InboxWake(rig.bus)
    assert default.ledger.service is True
    item, _ = send(rig)
    key = rig.reactor._key("sw", item.id)
    stored = rig.bus.state("inbox-wake").get(key)
    assert stored is not None


def test_outbox_retry_preserves_every_message_field_and_resolves_the_actual_recipient(rig, monkeypatch):
    from scripts.swarm.naming import NameRegistry

    monkeypatch.setattr(NameRegistry, "slug_of", lambda self, name: "sw" if name == "worker" else "")
    item = rig.inbox.send("sender", "worker", "preserve payload", ref="ledger:1", fyi=True, task="task42")
    rig.reactor.flush()
    [delivery] = rig.bus.events.consumer("payload-proof", "swarm.sw.inbox.*").fetch(10, 0)
    assert delivery.event.data == {
        "id": item.id,
        "sender": "sender",
        "text": "preserve payload",
        "ref": "ledger:1",
        "fyi": True,
        "task": "task42",
    }


def test_outbox_retry_is_bounded_to_thirty_two_messages_per_pass(rig):
    for number in range(33):
        rig.inbox.send("sender", "eng-1@sw", f"request {number}")
    rig.reactor.flush()
    assert rig.inbox.redis.scard(rig.inbox.key("wake-outbox")) == 1
    rig.reactor.flush()
    assert rig.inbox.redis.scard(rig.inbox.key("wake-outbox")) == 0


def test_outbox_reuses_the_send_of_an_item_readdressed_after_its_first_send(rig):
    item = rig.inbox.send("sender", "eng-1@sw", "moved request")
    rig.reactor.flush()
    pump(rig)
    seat = rig.bus.events.consumer("moved-proof", "swarm.sw.inbox.*")
    sent = seat.fetch(10, 0)
    for delivery in sent:
        delivery.ack()
    rig.inbox.redirect(item.id, "swarm", "eng-2@sw", "seat moved")
    rig.inbox.seats.occupy("eng-2@sw", "worker", at=1000)
    rig.inbox.redis.sadd(rig.inbox.key("wake-outbox"), item.id)
    for now in range(1001, 1006):
        rig.clock.now = now
        rig.reactor.flush()
    pump(rig)
    assert rig.inbox.redis.scard(rig.inbox.key("wake-outbox")) == 0
    assert [delivery.event.key for delivery in sent] == ["eng-1@sw"]
    assert seat.fetch(10, 0) == []
    assert steps(rig, item) == []
    advance(rig, 1000 + W)
    assert steps(rig, item) == ["woken"]
    assert rig.inbox.get(item.id).address == "eng-2@sw"


def test_an_item_readdressed_to_another_swarm_publishes_its_wake_there(rig):
    item = rig.inbox.send("sender", "eng-1@sw", "moved across swarms", ref="ledger:2", task="t9")
    rig.reactor.flush()
    rig.inbox.redis.hset(rig.inbox.key("item", item.id), "address", "eng-1@other")
    rig.inbox.redis.sadd(rig.inbox.key("wake-outbox"), item.id)
    rig.clock.now = 4242
    rig.reactor.flush()
    [delivery] = rig.bus.events.consumer("other-proof", "swarm.other.inbox-wake.requested").fetch(10, 0)
    digest = hashlib.sha256("other\0eng-1@other".encode()).hexdigest()
    assert delivery.event.to_dict() == {
        "id": f"wake-{item.id}-{digest[:16]}",
        "type": "inbox.wake",
        "swarm": "other",
        "entity": "inbox-wake",
        "key": item.id,
        "version": 1,
        "time": 1000,
        "author": "sender",
        "data": {"address": "eng-1@other"},
        "cause": None,
        "schema_version": 1,
    }
    assert rig.inbox.redis.scard(rig.inbox.key("wake-outbox")) == 0


def test_outbox_keeps_an_invalid_swarm_address_for_a_later_repair(rig):
    item = rig.inbox.send("sender", "eng-1@sw.part@elsewhere", "invalid swarm address")
    with pytest.raises(ValueError, match="invalid event subject token"):
        rig.reactor.flush()
    assert rig.inbox.redis.smembers(rig.inbox.key("wake-outbox")) == {item.id}


def test_an_item_that_fails_every_pass_does_not_hold_back_the_rest(rig):
    bad = rig.inbox.send("sender", "eng-1@sw.part@elsewhere", "invalid swarm address")
    for number in range(3):
        rig.inbox.send("sender", "eng-1@sw", f"request {number}")
    for now in range(2000, 2050):
        rig.clock.now = now
        with pytest.raises(ValueError, match="invalid event subject token"):
            rig.reactor.flush()
    assert rig.inbox.redis.smembers(rig.inbox.key("wake-outbox")) == {bad.id}


@pytest.mark.parametrize("state", ["read", "confirmed", "done", "fyi"])
def test_settled_or_information_items_cancel_their_timer(rig, state):
    item = rig.inbox.send("sender", "worker", "request", fyi=state == "fyi")
    SeatInbox(rig.bus, "sw").send(item.address, Message(item.id, item.sender, item.text, fyi=item.fyi))
    pump(rig)
    if state == "read":
        rig.inbox.read(item.id, "worker")
    elif state == "confirmed":
        rig.inbox.deliver(item.id, "worker")
        rig.inbox.confirm(item.id, "worker")
    elif state == "done":
        rig.inbox.close(item.id, "worker", "done", "handled")
    advance(rig, 1000)
    [cancelled] = rig.bus.events.consumer("cancel-proof", "swarm.*.deadline.cancelled").fetch(10, 0)
    assert cancelled.event.data["entity"] == "inbox-wake"
    assert rig.herdr.prompts == []
    assert rig.ledger.followups == []


def test_a_missing_message_cancels_cleanly(rig):
    SeatInbox(rig.bus, "sw").send("worker", Message("missing", "sender", "request"))
    pump(rig)
    advance(rig, 1000)
    [cancelled] = rig.bus.events.consumer("missing-proof", "swarm.*.deadline.cancelled").fetch(10, 0)
    assert cancelled.event.data["key"] == "missing"
    assert rig.herdr.prompts == []


def test_operator_escalation_cancels_the_timer_with_a_stable_target(rig):
    item, _ = send(rig, "boss")
    [original] = rig.bus.events.consumer("original-proof", "swarm.*.deadline.scheduled").fetch(10, 0)
    advance(rig, 1000 + W)
    [cancelled] = rig.bus.events.consumer("completed-proof", "swarm.*.deadline.cancelled").fetch(10, 0)
    assert cancelled.event.key == original.event.key
    assert cancelled.event.data == {"kind": "inbox", "entity": "inbox-wake", "key": item.id, "deadline_ms": None}


def test_unknown_expiry_cause_or_version_cannot_advance_a_message(rig):
    from scripts.events import deadlines

    item, _ = send(rig)
    [request] = rig.bus.events.consumer("fence-proof", "swarm.*.deadline.scheduled").fetch(10, 0)
    forged = replace(deadlines.expired(request.event), id="forged", cause="wrong", version=99)
    rig.reactor.handle(forged)
    assert steps(rig, item) == []
    assert rig.herdr.prompts == []


@pytest.mark.parametrize("redelivery", ["1", "1000"])
def test_redelivery_window_uses_the_reactors_environment(rig, redelivery):
    rig.reactor.environ = {"AGENTIHOOKS_INBOX_REDELIVER_S": redelivery}
    item, _ = send(rig)
    rig.inbox.deliver(item.id, "worker")
    advance(rig, 1000)
    requests = rig.bus.events.consumer("redelivery-proof", "swarm.*.deadline.scheduled").fetch(10, 0)
    assert requests[-1].event.data["deadline_ms"] == (1000 + W if redelivery == "1" else 1_001_000)
    advance(rig, 1000 + W)
    assert rig.inbox.get(item.id).state == ("pending" if redelivery == "1" else "delivered")
    if redelivery == "1":
        pending = [row for row in rig.inbox.history(item.id) if row.get("reason", "").startswith("redelivered")]
        assert len(pending) == 1
        assert pending[0]["by"] == "inbox"
        assert pending[0]["reason"] == "redelivered: never confirmed inside the redelivery window"


def test_late_first_expiry_keeps_a_delivery_inside_its_custom_confirmation_window(rig):
    rig.reactor.environ = {"AGENTIHOOKS_INBOX_REDELIVER_S": "1000"}
    item, _ = send(rig)
    rig.inbox.deliver(item.id, "worker")
    advance(rig, 1000 + W)
    assert rig.inbox.get(item.id).state == "delivered"
    assert steps(rig, item) == []
    assert rig.herdr.prompts == []


def test_custom_quiet_window_protects_a_recent_operator_prompt(rig):
    from scripts.swarm import idle

    rig.reactor.environ = {"AGENTIHOOKS_INBOX_QUIET_S": "7"}
    item, _ = send(rig)
    idle.prompted(rig.inbox.redis, "sw", "worker", 999)
    advance(rig, 1000)
    assert rig.herdr.prompts == []
    assert steps(rig, item) == []


def test_finished_panes_are_not_woken(rig):
    agents = rig.reactor.store.agents("sw")
    rig.reactor.store.agents = lambda slug: [replace(agents[0], state="finished"), agents[1]]
    item, _ = send(rig)
    advance(rig, 1000)
    assert rig.herdr.prompts == []
    assert steps(rig, item) == []


def test_partial_outage_keeps_the_remaining_window_instead_of_restarting_it(rig):
    from scripts.swarm import outage

    rig.herdr.status["p1"] = "working"
    item, _ = send(rig)
    outage.observe("sw", "provider", ["worker (codex): stream disconnected before completion"], 1100)
    outage.observe("sw", "provider", [], 1100 + W)
    advance(rig, 1000 + W + 100)
    requests = rig.bus.events.consumer("remaining-proof", "swarm.*.deadline.scheduled").fetch(10, 0)
    assert requests[-1].event.data["deadline_ms"] == 1000 + 2 * W
    advance(rig, 1000 + 2 * W)
    assert steps(rig, item) == ["escalated_master"]


def test_seat_binding_mid_window_uses_the_binding_clock(rig):
    item, _ = send(rig, "eng-1@sw")
    advance(rig, 1000)
    rig.inbox.seats.occupy("eng-1@sw", "worker", at=1000 + W // 2)
    rig.herdr.status["p1"] = "working"
    advance(rig, 1000 + W)
    requests = rig.bus.events.consumer("binding-proof", "swarm.*.deadline.scheduled").fetch(10, 0)
    assert requests[-1].event.data["deadline_ms"] == 1000 + W + W // 2
    advance(rig, 1000 + W + W // 2)
    assert steps(rig, item) == ["escalated_master"]


def test_stopped_swarm_seat_mail_does_not_wait_for_an_unavailable_successor(rig):
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(rig.inbox.redis)
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1, state="stopped"))
    rig.reactor.store = store
    item, _ = send(rig, "eng-1@sw")
    advance(rig, 1000 + W // 2)
    advance(rig, 1000 + W)
    assert rig.ledger.flagged == [True]


def test_inbox_maintenance_keeps_redelivery_actions_from_each_world(rig, monkeypatch):
    from types import SimpleNamespace

    first = SimpleNamespace(pending=lambda: [], redeliver=lambda now, window: [SimpleNamespace(id="first")])
    second = SimpleNamespace(pending=lambda: [], redeliver=lambda now, window: [SimpleNamespace(id="second")])
    monkeypatch.setattr(rig.inbox, "worlds", lambda: [first, second])
    monkeypatch.setattr(wake.addresses, "settle_unresolved", lambda *args: [])
    assert wake.maintenance(rig.inbox, "sw", [], 1000, W) == [
        "redelivered message first",
        "redelivered message second",
    ]


def test_non_inbox_deadlines_and_wrong_arrival_entities_are_ignored(rig):
    from scripts.events import deadlines

    item, _ = send(rig)
    [arrival] = rig.bus.events.consumer("wrong-arrival-proof", "swarm.*.inbox-wake.requested").fetch(10, 0)
    [request] = rig.bus.events.consumer("wrong-kind-proof", "swarm.*.deadline.scheduled").fetch(10, 0)
    before = len(rig.bus.events.entries)
    rig.reactor.handle(replace(arrival.event, id="wrong-entity", entity="other", key="other-item"))
    rig.reactor.handle(replace(arrival.event, id="wrong-type", type="execution.changed", key="another-item"))
    expired = deadlines.expired(request.event)
    rig.reactor.handle(replace(expired, data={**expired.data, "kind": "idle"}))
    assert len(rig.bus.events.entries) == before
    assert steps(rig, item) == []
