import pytest

from scripts.inbox import exits
from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group(name="fakeredis")]


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


@pytest.mark.parametrize("address", ["sw-eng-1", "eng-1@sw"])
@pytest.mark.parametrize("state", ["pending", "confirmed"])
def test_overrun_request_ends_with_its_departing_life(redis, address, state):
    inbox, store = InboxStore(redis), RedisStore(redis)
    store.seats.occupy("eng-1@sw", "sw-eng-1", 1)
    item = inbox.send("swarm", address, "Push and hand off task t1.", ref="sw:overrun:t1", task="t1")
    if state == "confirmed":
        inbox.deliver(item.id, "sw-eng-1")
        inbox.confirm(item.id, "sw-eng-1")
    exits.settle(inbox, "sw-eng-1", "eng-1@sw", "handed off its seat")
    if address == "eng-1@sw" and state == "pending":
        exits.sweep(inbox, "sw", store, dict)
    after = inbox.get(item.id)
    assert after.state == "done"
    assert after.reason == "done: sw-eng-1 handed off its seat; its overrun request ended with it"


@pytest.mark.parametrize("address", ["sw-eng-1", "eng-1@sw"])
@pytest.mark.parametrize("received", [False, True])
def test_task_mail_is_withdrawn_when_the_seat_holds_other_work(redis, address, received):
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    departed = AgentRecord(name="sw-eng-1", lane="eng", task="t1", seat="eng-1@sw", state="finished")
    successor = AgentRecord(name="sw-eng-2", lane="eng", task="t2", seat=departed.seat)
    store.record_launch("sw", departed, "started")
    store.put_agent("sw", departed)
    store.seats.occupy(departed.seat, departed.name, 1)
    item = inbox.send("sender", address, "Complete task t1.", task="t1")
    general = inbox.send("sender", address, "Keep the seat instructions.")
    if received:
        inbox.deliver(item.id, departed.name)
        inbox.confirm(item.id, departed.name)
    store.put_agent("sw", successor)
    store.seats.occupy(departed.seat, successor.name, item.created_at + 1)
    exits.settle(inbox, departed.name, departed.seat, "handed off its seat")
    if address == departed.seat:
        exits.sweep(inbox, "sw", store, dict)
    assert inbox.get(item.id).state == "cancelled"
    assert inbox.get(general.id).state == "pending"
    [notice] = inbox.inbox("sender")
    assert notice.fyi
    assert item.id in notice.text


@pytest.mark.parametrize("address", ["sw-eng-1", "eng-1@sw"])
def test_same_task_continuation_keeps_its_task_mail(redis, address):
    from scripts.swarm.store import AgentRecord

    inbox, store = InboxStore(redis), RedisStore(redis)
    departed = AgentRecord(name="sw-eng-1", lane="eng", task="t1", seat="eng-1@sw", state="finished")
    successor = AgentRecord(name="sw-eng-2", lane="eng", task="t1", seat=departed.seat)
    store.record_launch("sw", departed, "started")
    store.put_agent("sw", departed)
    store.seats.occupy(departed.seat, departed.name, 1)
    item = inbox.send("sender", address, "Complete task t1.", task="t1")
    inbox.deliver(item.id, departed.name)
    store.put_agent("sw", successor)
    store.seats.occupy(departed.seat, successor.name, item.created_at + 1)
    exits.settle(inbox, departed.name, departed.seat, "handed off its seat")
    exits.sweep(inbox, "sw", store, dict)
    after = inbox.get(item.id)
    assert (after.address, after.state) == (departed.seat, "pending")
    assert inbox.inbox("sender") == []
