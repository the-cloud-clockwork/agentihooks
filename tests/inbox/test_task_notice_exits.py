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
