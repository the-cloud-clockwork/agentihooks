import fakeredis
import pytest

from scripts.hive import handoff_order, remove
from scripts.inbox import exits
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig


@pytest.fixture
def store() -> RedisStore:
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
