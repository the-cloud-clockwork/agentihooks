from dataclasses import replace

import pytest

from scripts.events import nats
from scripts.events.bus import StateConflict
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from scripts.events.subjects import subject
from scripts.hive import nats_grant
from tests.hive.test_nats_grant import _user_creds, _violations
from tests.hive.test_nats_grant import signer as signer

pytestmark = pytest.mark.unit
POLICY = QueuePolicy(max_deliver=1, ack_wait_ms=100)


def event(swarm="rig", identity="own"):
    return Envelope(identity, "execution.started", swarm, "execution", "work", 1, 0, "member", {})


@pytest.fixture
def hub(signer):
    bus = nats.open_bus(
        BusConfig(
            backend="nats", url=signer.url, scoped=True, credentials=_user_creds(signer.account, "hub"), queue=POLICY
        )
    )
    from tests.events.test_nats_state import guard_state_reads

    guard_state_reads(bus._js)
    try:
        yield bus
    finally:
        bus.close()


def member(signer, swarm="rig"):
    from tests.events.test_nats_state import guard_state_reads

    configured = replace(signer, member_swarms={"m1": (swarm,)})
    client = nats.open_bus(
        BusConfig(
            backend="nats",
            url=signer.url,
            scoped=True,
            provision=False,
            credentials=nats_grant.mint(configured, "m1"),
            queue=POLICY,
        )
    )
    guard_state_reads(client._js)
    return client


def test_member_scoped_bus_logs_queues_and_keeps_state(hub, signer):
    from scripts.events.nats_scope import NatsScopedBus
    from scripts.events.nats_state import ScopedStateStore

    own = hub.for_swarm("rig")
    assert isinstance(own, NatsScopedBus)
    assert hub.for_swarm("rig") is own
    own.queue("eng", POLICY)
    own.state("spoke-work")
    own.events.consumer("member-events", "swarm.rig.>")
    own.dead.consumer("member-dead", "dead.>")
    client = member(signer)
    try:
        scoped = client.for_swarm("rig")
        queue = scoped.queue("swarm.rig.ready.eng", POLICY)
        work = event()
        assert queue.offer(work)
        delivery = queue.take(1000)
        assert delivery.event == work
        delivery.ack()
        assert scoped.events.publish(subject("rig", "execution", "started"), work)
        [logged] = scoped.events.consumer("member-events", "swarm.rig.>").fetch(1, 1000)
        assert logged.event == work
        logged.ack()
        state = scoped.state("spoke-work")
        assert isinstance(state, ScopedStateStore)
        revision = state.create("key", b"first")
        updated = state.update("key", b"second", revision)
        assert state.get("key").value == b"second"
        assert state.get("key").revision == updated
        assert [(entry.value, entry.revision) for entry in state.watch("key")] == [
            (b"first", revision),
            (b"second", updated),
        ]
        with pytest.raises(StateConflict):
            state.update("key", b"stale", revision)
        assert scoped.dead.publish("dead.rig", work)
        [dead] = scoped.dead.consumer("member-dead", "dead.>").fetch(1, 1000)
        assert dead.event == work
        dead.ack()
    finally:
        client.close()


def test_scoped_queue_attempt_cap_moves_work_to_its_own_dead_stream(hub, signer):
    own = hub.for_swarm("rig")
    own.queue("eng", POLICY).offer(event())
    own.dead.consumer("attempts", "dead.>")
    client = member(signer)
    try:
        scoped = client.for_swarm("rig")
        queue = scoped.queue("eng", POLICY)
        delivery = queue.take(1000)
        assert delivery.attempt == 1
        delivery.nak()
        assert queue.take(1000) is None
        [dead] = scoped.dead.consumer("attempts", "dead.>").fetch(1, 1000)
        assert dead.event.cause == "own"
        assert dead.event.data["attempts"] == 1
        dead.ack()
    finally:
        client.close()


@pytest.mark.parametrize(
    "target",
    [
        "swarm.other.queue.eng.work",
        "swarm.other.dead.letter",
        "swarm.other.state.spoke-work.key",
        "$JS.API.STREAM.MSG.GET.AGENTIHOOKS_6f74686572_STATE_spoke-work",
        "$JS.API.CONSUMER.MSG.NEXT.AGENTIHOOKS_6f74686572_QUEUE_eng.eng",
    ],
)
def test_member_is_refused_foreign_resources_that_exist(hub, signer, target):
    foreign = hub.for_swarm("other")
    foreign.queue("eng", POLICY).offer(event("other", "foreign"))
    foreign.state("spoke-work").create("key", b"foreign")
    foreign.dead.publish("dead.other", event("other", "foreign"))
    configured = replace(signer, member_swarms={"m1": ("rig",)})

    async def act(nc):
        await nc.publish(target, b"{}")

    assert _violations(configured, nats_grant.mint(configured, "m1"), act, refused=True) == [
        f'nats: permissions violation for publish to "{target.lower()}"'
    ]


def test_legacy_bus_and_scoped_bus_keep_working_on_the_same_server(signer):
    legacy = nats.open_bus(BusConfig(backend="nats", url=signer.url, credentials=_user_creds(signer.account, "legacy")))
    legacy_queue = legacy.queue("swarm.old.ready.eng", POLICY)
    old = event("old", "old")
    legacy_queue.offer(old)
    legacy.state("spoke-work").create("old", b"legacy")
    modern = nats.open_bus(
        BusConfig(backend="nats", url=signer.url, scoped=True, credentials=_user_creds(signer.account, "modern"))
    )
    try:
        modern.migrate_state("rig", "spoke-work", [])
        scoped = modern.for_swarm("rig")
        scoped.queue("eng", POLICY).offer(event())
        scoped.state("spoke-work").create("old", b"scoped")
        assert legacy.events.publish(subject("old", "task", "ready"), old)
        assert legacy_queue.take(1000).event == old
        assert legacy.state("spoke-work").get("old").value == b"legacy"
        assert scoped.state("spoke-work").get("old").value == b"scoped"
        assert scoped.queue("eng", POLICY).take(1000).event == event()
    finally:
        modern.close()
        legacy.close()


def test_hub_fans_in_scoped_event_streams_with_existing_reactor_filters(hub):
    inventory = hub._js.streams_info_iterator

    async def bounded_inventory(*, offset):
        assert isinstance(offset, int) and offset >= 0
        return await inventory(offset=offset)

    hub._js.streams_info_iterator = bounded_inventory
    consumer = hub.events.consumer("aggregate", "swarm.*.execution.started")
    assert hub.events.publish(subject("rig", "execution", "started"), event())
    assert hub.events.publish(subject("other", "execution", "started"), event("other", "second"))
    deliveries = consumer.fetch(10, 1000)
    assert {delivery.event.swarm for delivery in deliveries} == {"rig", "other"}
    for delivery in deliveries:
        delivery.ack()


def test_member_state_exists_before_its_first_event(hub, signer):
    from scripts.hive import buckets

    hub.for_swarm("rig")
    client = member(signer)
    try:
        own = client.for_swarm("rig")
        for bucket in buckets.NAMES:
            assert own.state(bucket).get("missing") is None
        assert own.state("hive-presence").create("hive-a", b"present")
    finally:
        client.close()


def test_nats_aggregate_consumers_preserve_configured_policy(hub):
    consumer = hub.events.consumer("configured-events", "swarm.>")
    assert hub.events.consumer("configured-events", "swarm.>", POLICY) is consumer
    dead = hub.dead.consumer("configured-dead", "dead.>")
    assert hub.dead.consumer("configured-dead", "dead.>", POLICY) is dead


def test_scoped_nats_root_routes_queues_and_dead_letters(hub):
    own = hub.for_swarm("rig")
    queue = hub.queue("swarm.rig.ready.eng", POLICY)
    assert queue is own.queue("eng", POLICY)
    assert queue._subject == "swarm.rig.queue.eng.work"
    assert queue._filter == "swarm.rig.queue.eng.>"
    assert queue.policy == POLICY
    assert queue.offer(event())
    assert queue.take(1000).event == event()
    for name in ["eng", "swarm.rig.ready.eng.extra", "other.rig.ready.eng", "swarm.rig.other.eng"]:
        with pytest.raises(ValueError) as caught:
            hub.queue(name, POLICY)
        assert str(caught.value) == "select a swarm before opening a scoped queue"
    consumer = hub.dead.consumer("aggregate-dead", "dead.>")
    assert hub.dead.publish("dead.rig", event())
    [dead] = consumer.fetch(1, 1000)
    assert dead.event == event()
    dead.ack()


def test_scoped_member_refuses_root_state_and_pending_queue_import(hub, signer):
    from scripts.events.nats_state import IMPORT_PENDING
    from scripts.events.subjects import stream_name

    own = hub.for_swarm("rig")
    queue = own.queue("eng", POLICY)
    info = hub._loop.run(hub._js.stream_info(queue._stream))
    hub._loop.run(hub._js.update_stream(config=replace(info.config, metadata={IMPORT_PENDING: "queue"})))
    client = member(signer)
    try:
        with pytest.raises(PermissionError) as caught:
            client.state("spoke-work")
        assert str(caught.value) == "select a swarm before opening scoped state"
        with pytest.raises(RuntimeError) as caught:
            client.for_swarm("rig").queue("eng", POLICY)
        assert str(caught.value) == "scoped queue migration is incomplete"
        assert queue._stream == stream_name("rig", "QUEUE_eng")
    finally:
        client.close()
    client = member(signer)
    client.close()
    with pytest.raises(RuntimeError) as caught:
        client.for_swarm("rig")
    assert str(caught.value) == "bus is closed"


def test_scoped_event_consumers_dead_letter_within_their_own_swarm(hub, signer):
    own = hub.for_swarm("rig")
    own.events.consumer("capped-events", "swarm.rig.>", POLICY)
    own.dead.consumer("capped-dead", "dead.>", POLICY)
    client = member(signer)
    try:
        view = client.for_swarm("rig")
        consumer = view.events.consumer("capped-events", "swarm.rig.>", POLICY)
        assert view.events.publish("swarm.rig.ready", event())
        [delivery] = consumer.fetch(1, 1000)
        delivery.nak()
        assert consumer.fetch(1, 1000) == []
        [dead] = view.dead.consumer("capped-dead", "dead.>", POLICY).fetch(1, 1000)
        assert dead.event.cause == "own"
        dead.ack()
    finally:
        client.close()


def test_a_scoped_member_reaches_the_hub_slot_service_only_through_its_own_swarm(hub, signer):
    from scripts.hive import account_slots
    from scripts.hub import slots

    slots.serve(hub, clock=lambda: 1000)
    report = {"slots": [{"harness": "claude", "account": "a", "cap": 1}], "sessions": {"claude:a": 0}}
    client = member(signer)
    try:
        own = account_slots.Remote(account_slots.asker(client.for_swarm("rig"), "rig"), "m1", lambda: report)
        assert own.take("one", "claude") == "a"
        foreign = account_slots.Remote(account_slots.asker(client, "other", 500), "m1", lambda: report)
        with pytest.raises(TimeoutError, match="^no responders for 'swarm.other.slots'$"):
            foreign.take("two", "claude")
        own.release("one", "claude", "a")
        assert own.take("two", "claude") == "a"
    finally:
        client.close()


def test_scoped_members_open_no_root_state_including_the_hub_owned_account_buckets(hub, signer):
    from scripts.hub import slots

    first = member(signer, "rig")
    try:
        for bucket in (
            "spoke-work",
            "spoke-pending",
            "hive-presence",
            "hive-executions",
            "execution-starts",
            *slots.BUCKETS,
            "unlisted",
        ):
            with pytest.raises(PermissionError) as caught:
                first.state(bucket)
            assert str(caught.value) == "select a swarm before opening scoped state"
    finally:
        first.close()
