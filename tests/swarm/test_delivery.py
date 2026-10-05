import json

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import delivery
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")


class FakeHerdr:
    def __init__(self, status):
        self.status, self.prompts = dict(status), []

    def agent_status(self, agent):
        return self.status.get(agent.pane_id, "unknown")

    def prompt(self, agent, text):
        self.prompts.append((agent.pane_id, text))


class PageLedger:
    def __init__(self):
        self.said = []

    def say(self, slug, text, by=None):
        self.said.append((text, by))


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", 2, 1))
    for name, lane, pane in (("sw-eng-1", "eng", "p1"), ("sw-eng-2", "eng", "p2"), ("sw-ci-1", "ci", "p3")):
        s.put_agent("sw", AgentRecord(name, lane, "t", pane_id=pane))
    return s


def inbox(store, address):
    return [(i.sender, i.text, i.state) for i in InboxStore(store.redis).inbox(address)]


def test_addressing_by_all_lane_and_name_skips_the_sender(store):
    names = lambda to, sender="": sorted(a.name for a in delivery.recipients(store, "sw", to, sender))  # noqa: E731
    assert names("", "sw-eng-1") == ["sw-ci-1", "sw-eng-2"]
    assert names("eng") == ["sw-eng-1", "sw-eng-2"]
    assert names("sw-ci-1") == ["sw-ci-1"]


def test_send_leaves_one_pending_inbox_item_per_recipient_from_the_real_sender(store):
    assert delivery.send(store, "sw", "merge the docs first", sender="sw-ci-1", to="eng") == ["sw-eng-1", "sw-eng-2"]
    for name in ("sw-eng-1", "sw-eng-2"):
        assert inbox(store, name) == [("sw-ci-1", "merge the docs first", "pending")]
    assert inbox(store, "sw-ci-1") == []


def test_operator_page_messages_become_inbox_items_once(store):
    chat = [
        {"id": "a", "by": "operator", "at": 10, "text": "@ci please look at the slow job"},
        {"id": "b", "by": "sw-eng-1", "at": 11, "text": "an agent line is not relayed"},
    ]
    assert delivery.relay_operator_chat(store, "sw", chat) == 1
    assert inbox(store, "sw-ci-1") == [("operator", "please look at the slow job", "pending")]
    assert delivery.relay_operator_chat(store, "sw", chat) == 0
    assert len(inbox(store, "sw-ci-1")) == 1


def test_a_new_swarm_does_not_replay_old_chat(store):
    old = [{"id": "a", "by": "operator", "at": 10, "text": "old plan talk"}]
    delivery.start_cursor(store, "sw", old)
    assert delivery.relay_operator_chat(store, "sw", old) == 0
    assert all(inbox(store, a.name) == [] for a in store.agents("sw"))


def test_an_unknown_addressee_goes_to_everyone_with_a_note(store):
    delivery.relay_operator_chat(store, "sw", [{"id": "a", "by": "operator", "at": 5, "text": "@nobody hi"}])
    for agent in store.agents("sw"):
        [(sender, text, _)] = inbox(store, agent.name)
        assert sender == "operator" and "not in the swarm" in text


def test_unaddressed_operator_chat_goes_to_the_master_when_one_is_online(store):
    store.put_agent("sw", AgentRecord("sw-master-1", "master", "master", pane_id="m1"))
    chat = [
        {"id": "a", "by": "operator", "at": 5, "text": "how far along are we"},
        {"id": "b", "by": "operator", "at": 6, "text": "@eng rebase on dev"},
        {"id": "c", "by": "operator", "at": 7, "text": "@nobody hi"},
    ]
    delivery.relay_operator_chat(store, "sw", chat)
    master = inbox(store, "sw-master-1")
    assert master[0] == ("operator", "how far along are we", "pending")
    assert len(master) == 2 and "not in the swarm" in master[1][1]
    assert inbox(store, "sw-eng-1") == inbox(store, "sw-eng-2") == [("operator", "rebase on dev", "pending")]
    assert inbox(store, "sw-ci-1") == []


def test_a_finished_master_does_not_take_the_chat(store):
    store.put_agent("sw", AgentRecord("sw-master-1", "master", "master", pane_id="m1", state="finished"))
    delivery.relay_operator_chat(store, "sw", [{"id": "a", "by": "operator", "at": 5, "text": "hi"}])
    assert inbox(store, "sw-master-1") == []
    assert all(inbox(store, n) == [("operator", "hi", "pending")] for n in ("sw-eng-1", "sw-eng-2", "sw-ci-1"))


def test_items_for_the_operator_from_swarm_agents_are_shown_on_the_page_and_closed(store):
    box = InboxStore(store.redis)
    reply = box.send("sw-eng-1", "operator", "the slow job is fixed")
    stranger = box.send("someone-else", "operator", "not from this swarm")
    ledger = PageLedger()
    agents = store.agents("sw")
    assert delivery.relay_to_page(box, "sw", agents, ledger) == 1
    assert ledger.said == [("the slow job is fixed", "sw-eng-1")]
    assert box.get(reply.id).state == "done" and "ledger page" in box.get(reply.id).reason
    assert box.get(stranger.id).state == "pending"
    assert delivery.relay_to_page(box, "sw", agents, ledger) == 0


def test_messages_left_in_the_old_outbox_move_into_the_inbox(store):
    outbox = store.key("sw", "outbox")
    store.redis.rpush(outbox, json.dumps({"to": "sw-ci-1", "at": 5, "text": "[swarm chat] operator: rerun the job"}))
    store.redis.rpush(outbox, json.dumps({"to": "sw-eng-1", "at": 6, "text": "no prefix here"}))
    assert delivery.migrate_outbox(store, "sw", InboxStore(store.redis)) == 2
    assert inbox(store, "sw-ci-1") == [("operator", "rerun the job", "pending")]
    assert inbox(store, "sw-eng-1") == [("swarm", "no prefix here", "pending")]
    assert not store.redis.exists(outbox)


def test_an_outbox_entry_stays_when_moving_it_fails(store):
    class Down:
        def send(self, sender, address, text):
            raise ConnectionError("redis went away")

    outbox = store.key("sw", "outbox")
    store.redis.rpush(outbox, json.dumps({"to": "sw-ci-1", "at": 5, "text": "[swarm chat] operator: rerun the job"}))
    with pytest.raises(ConnectionError):
        delivery.migrate_outbox(store, "sw", Down())
    assert store.redis.llen(outbox) == 1
