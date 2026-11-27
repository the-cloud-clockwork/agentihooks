import pytest

from scripts.hive import handoff_order, remove
from scripts.inbox import bound, exits, seen
from scripts.inbox.dispatch import Dispatcher
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    result = RedisStore(redis)
    result.create(SwarmConfig("sw", "agentihooks", 1, 0))
    return result


def life(store: RedisStore, lane: str, previous: AgentRecord | None = None) -> AgentRecord:
    seat = "master@sw" if lane == "master" else "eng-1@sw"
    agent = store.start_execution(
        "sw",
        AgentRecord(store.next_name("sw", lane), lane, "t1", seat=seat, started_at=100),
        previous.execution_id if previous else "",
    )
    store.seats.occupy(seat, agent.name, agent.generation)
    return agent


def order(inbox: InboxStore, agent: AgentRecord) -> str:
    ordered: dict[tuple[str, str], str] = {}
    remove._order(inbox, ordered, [("sw", agent)], handoff_order.ROLL, "hive-one")
    return ordered[("sw", agent.name)]


@pytest.mark.feature("inbox-execution-bound-orders")
@pytest.mark.parametrize("lane", ["master", "eng"])
def test_a_successor_does_not_read_the_previous_executions_hive_order(store: RedisStore, lane: str) -> None:
    previous = life(store, lane)
    inbox = InboxStore(store.redis)
    item_id = order(inbox, previous)
    if lane == "eng":
        inbox.redirect(item_id, handoff_order.SENDER, previous.seat, "work continues in the seat", previous.name)
    successor = life(store, lane, previous)
    assert inbox.pending_mail(successor.name) == []
    assert inbox.get(item_id).state == "done"


@pytest.mark.feature("inbox-execution-bound-orders")
@pytest.mark.parametrize("lane", ["master", "eng"])
def test_a_live_execution_still_receives_its_hive_order(store: RedisStore, lane: str) -> None:
    agent = life(store, lane)
    inbox = InboxStore(store.redis)
    item_id = order(inbox, agent)
    assert [item.id for item in inbox.pending_mail(agent.name)] == [item_id]
    assert inbox.deliver(item_id, agent.name) is not None


@pytest.mark.feature("inbox-execution-bound-orders")
@pytest.mark.parametrize("lane", ["master", "eng"])
def test_unbound_seat_mail_still_reaches_the_next_occupant(store: RedisStore, lane: str) -> None:
    previous = life(store, lane)
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", previous.seat, "Continue the seat work")
    successor = life(store, lane, previous)
    assert [pending.id for pending in inbox.pending_mail(successor.name)] == [item.id]
    assert inbox.get(item.id).state == "pending"


@pytest.mark.feature("inbox-execution-bound-orders")
@pytest.mark.parametrize("taken", [False, True])
def test_the_exit_settlement_closes_a_masters_bound_order(store: RedisStore, taken: bool) -> None:
    agent = life(store, "master")
    inbox = InboxStore(store.redis)
    item_id = order(inbox, agent)
    if taken:
        assert inbox.deliver(item_id, agent.name) is not None
    exits.settle(inbox, agent.name, agent.seat, "handed off its seat")
    assert inbox.get(item_id).state == "done"


@pytest.mark.feature("inbox-execution-bound-orders")
def test_an_ended_execution_does_not_receive_its_order_before_the_sweep(store: RedisStore) -> None:
    agent = life(store, "master")
    inbox = InboxStore(store.redis)
    item_id = order(inbox, agent)
    assert store.drop_agent("sw", agent.name, expected=agent)
    assert inbox.pending_mail(agent.name) == []
    assert inbox.get(item_id).state == "done"


@pytest.mark.feature("inbox-execution-bound-orders")
def test_a_bound_order_keeps_its_execution_when_routed_and_loaded(store: RedisStore) -> None:
    agent = life(store, "eng")
    sender = InboxStore(store.redis, world="another-world")
    sent = sender.send(
        "operator",
        bound.Recipient(agent.name, agent.execution_id),
        "Finish this step",
        "ref",
        False,
        "t1",
        "fixed-item",
    )
    loaded = InboxStore(store.redis).get(sent.id)
    assert (loaded.id, loaded.execution, loaded.address, loaded.ref, loaded.task) == (
        "fixed-item",
        agent.execution_id,
        agent.name,
        "ref",
        "t1",
    )


@pytest.mark.feature("inbox-execution-bound-orders")
def test_a_legacy_item_with_no_execution_still_loads_unbound(store: RedisStore) -> None:
    agent = life(store, "eng")
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", agent.name, "Continue the work")
    store.redis.hdel(inbox.key("item", item.id), "execution")
    assert InboxStore(store.redis).get(item.id).execution == ""
    assert inbox.pending_mail(agent.name)[0].id == item.id


@pytest.mark.feature("inbox-execution-bound-orders")
def test_an_order_does_not_cross_executions_with_the_same_agent_name(store: RedisStore) -> None:
    previous = life(store, "eng")
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", bound.Recipient(previous.seat, previous.execution_id), "Finish this step")
    current = store.start_execution(
        "sw", AgentRecord(previous.name, "eng", "t1", seat=previous.seat), previous.execution_id
    )
    store.seats.occupy(current.seat, current.name, current.generation)
    assert inbox.pending_mail(current.name) == []
    assert inbox.get(item.id).state == "done"


@pytest.mark.feature("inbox-execution-bound-orders")
@pytest.mark.parametrize("change", ["unknown", "unoccupied", "different-occupant"])
def test_a_bound_order_with_no_current_recipient_is_closed(store: RedisStore, change: str) -> None:
    agent = life(store, "eng")
    inbox = InboxStore(store.redis)
    execution = "missing-execution" if change == "unknown" else agent.execution_id
    item = inbox.send("operator", bound.Recipient(agent.name, execution), "Finish this step")
    if change == "unoccupied":
        store.redis.delete(inbox.seats.key(agent.seat))
    elif change == "different-occupant":
        store.seats.occupy(agent.seat, store.next_name("sw", "eng"), 2)
    assert inbox.pending_mail(agent.name) == []
    assert inbox.get(item.id).state == "done"


@pytest.mark.feature("inbox-execution-bound-orders")
@pytest.mark.parametrize("transport", ["hook", "reservation"])
def test_a_seat_change_between_read_and_claim_does_not_deliver_the_old_order(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    previous = life(store, "eng")
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", bound.Recipient(previous.seat, previous.execution_id), "Finish this step")
    listed = inbox.pending_mail(previous.name)
    successor = life(store, "eng", previous)
    monkeypatch.setattr(inbox, "pending_mail", lambda _: listed)
    if transport == "hook":
        assert seen.claim(inbox, successor.name) == []
    else:
        dispatcher = Dispatcher(inbox)
        dispatcher.own(successor.name, "owner")
        assert dispatcher.reserve(successor.name, "owner") == []
    assert inbox.get(item.id).state == "done"


@pytest.mark.feature("inbox-execution-bound-orders")
def test_a_claimant_recipient_uses_the_current_execution_identity(store: RedisStore) -> None:
    agent = life(store, "eng")
    recipient = bound.recipient_for(store, agent.name)
    assert (recipient.name, recipient.execution) == (agent.name, agent.execution_id)
    unknown = bound.recipient_for(store, "unregistered")
    with pytest.raises(InboxError, match="recipient execution"):
        InboxStore(store.redis).send("operator", unknown, "Finish this step")


@pytest.mark.feature("inbox-execution-bound-orders")
@pytest.mark.parametrize("lane", ["master", "eng"])
def test_the_sweep_closes_generic_bound_seat_orders_after_a_recorded_departure(store: RedisStore, lane: str) -> None:
    agent = life(store, lane)
    inbox = InboxStore(store.redis)
    item = inbox.send("swarm", bound.Recipient(agent.seat, agent.execution_id), "Finish this step")
    store.seats.record_exit(agent.name, agent.seat, "left its seat")
    assert store.drop_agent("sw", agent.name, expected=agent)
    exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(item.id).state == "done"


@pytest.mark.feature("inbox-execution-bound-orders")
@pytest.mark.parametrize("address", ["name", "seat"])
def test_redirecting_a_live_bound_order_never_delivers_it_to_another_agent(store: RedisStore, address: str) -> None:
    intended = life(store, "eng")
    other = life(store, "master")
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", bound.Recipient(intended.name, intended.execution_id), "Finish this step")
    target = other.name if address == "name" else other.seat
    inbox.redirect(item.id, "operator", target, "changed recipient", intended.name)
    assert inbox.pending_mail(other.name) == []
    assert inbox.get(item.id).state == "done"
