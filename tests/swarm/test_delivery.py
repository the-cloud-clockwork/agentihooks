import fakeredis
import pytest

from scripts.swarm import delivery
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig


class FakeHerdr:
    def __init__(self, status):
        self.status, self.prompts = dict(status), []

    def agent_status(self, agent):
        return self.status.get(agent.pane_id, "unknown")

    def prompt(self, agent, text):
        self.prompts.append((agent.pane_id, text))


@pytest.fixture
def store():
    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", 2, 1))
    for name, lane, pane in (("sw-eng-1", "eng", "p1"), ("sw-eng-2", "eng", "p2"), ("sw-ci-1", "ci", "p3")):
        s.put_agent("sw", AgentRecord(name, lane, "t", pane_id=pane))
    return s


def test_addressing_by_all_lane_and_name_skips_the_sender(store):
    names = lambda to, sender="": sorted(a.name for a in delivery.recipients(store, "sw", to, sender))  # noqa: E731
    assert names("", "sw-eng-1") == ["sw-ci-1", "sw-eng-2"]
    assert names("eng") == ["sw-eng-1", "sw-eng-2"]
    assert names("sw-ci-1") == ["sw-ci-1"]


def test_idle_agents_get_it_now_busy_ones_on_a_later_flush(store):
    herdr = FakeHerdr({"p1": "idle", "p2": "working"})
    delivery.send(store, "sw", "merge the docs first", sender="operator", to="eng", herdr=herdr)
    assert herdr.prompts == [("p1", "[swarm chat] operator: merge the docs first")]
    herdr.status["p2"] = "done"
    delivery.flush(store, "sw", herdr)
    assert herdr.prompts[-1] == ("p2", "[swarm chat] operator: merge the docs first")
    delivery.flush(store, "sw", herdr)
    assert len(herdr.prompts) == 2


def test_messages_for_agents_that_left_are_dropped(store):
    herdr = FakeHerdr({"p3": "working"})
    delivery.send(store, "sw", "hello", sender="operator", to="sw-ci-1", herdr=herdr)
    store.drop_agent("sw", "sw-ci-1")
    delivery.flush(store, "sw", herdr)
    assert herdr.prompts == [] and store.redis.llen(store.key("sw", "outbox")) == 0


def test_a_herdr_failure_keeps_the_message_queued(store):
    class Broken(FakeHerdr):
        def prompt(self, agent, text):
            raise TimeoutError("herdr hung")

    herdr = Broken({"p3": "idle"})
    delivery.send(store, "sw", "hello", sender="operator", to="sw-ci-1", herdr=herdr)
    assert store.redis.llen(store.key("sw", "outbox")) == 1


def test_operator_page_messages_are_relayed_once_with_their_address(store):
    herdr = FakeHerdr({"p1": "idle", "p2": "idle", "p3": "idle"})
    chat = [
        {"id": "a", "by": "operator", "at": 10, "text": "@ci please look at the slow job"},
        {"id": "b", "by": "sw-eng-1", "at": 11, "text": "an agent line is not relayed"},
    ]
    assert delivery.relay_operator_chat(store, "sw", chat, herdr) == 1
    assert herdr.prompts == [("p3", "[swarm chat] operator: please look at the slow job")]
    assert delivery.relay_operator_chat(store, "sw", chat, herdr) == 0


def test_stale_messages_expire(store):
    herdr = FakeHerdr({"p3": "working"})
    delivery.send(store, "sw", "hello", sender="operator", to="sw-ci-1", herdr=herdr, now_ms=1_000)
    delivery.flush(store, "sw", herdr, now_ms=1_000 + delivery.EXPIRE_MS + 1)
    assert store.redis.llen(store.key("sw", "outbox")) == 0


def test_a_second_flusher_backs_off_while_one_runs(store):
    herdr = FakeHerdr({"p3": "idle"})
    store.redis.set(store.key("sw", "flush-lock"), "other")
    delivery.send(store, "sw", "hello", sender="operator", to="sw-ci-1", herdr=herdr)
    assert herdr.prompts == [] and store.redis.llen(store.key("sw", "outbox")) == 1


def test_a_new_swarm_does_not_replay_old_chat(store):
    herdr = FakeHerdr({"p1": "idle", "p2": "idle", "p3": "idle"})
    old = [{"id": "a", "by": "operator", "at": 10, "text": "old plan talk"}]
    delivery.start_cursor(store, "sw", old)
    assert delivery.relay_operator_chat(store, "sw", old, herdr) == 0 and herdr.prompts == []


def test_an_unknown_addressee_goes_to_everyone_with_a_note(store):
    herdr = FakeHerdr({"p1": "idle", "p2": "idle", "p3": "idle"})
    delivery.relay_operator_chat(store, "sw", [{"id": "a", "by": "operator", "at": 5, "text": "@nobody hi"}], herdr)
    assert len(herdr.prompts) == 3 and "not in the swarm" in herdr.prompts[0][1]
