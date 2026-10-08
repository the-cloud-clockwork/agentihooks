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

    def relay(self, slug, text, by):
        assert slug == "sw"
        self.said.append((text, by))


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", 2, 1))
    for name, lane, pane in (("sw-eng-1", "eng", "p1"), ("sw-eng-2", "eng", "p2"), ("sw-ci-1", "ci", "p3")):
        s.put_agent("sw", AgentRecord(name, lane, "t", pane_id=pane))
    return s


def test_inbox_wakes_in_a_long_named_swarm_read_and_prompt_the_engineer_pane():
    calls = []
    messenger = delivery.HerdrMessenger(herdr=lambda args: calls.append(args) or {"agent_status": "idle"})
    slug = "okay-we-re-going-to-mossy-rabin-2026-10-05"
    eng, master = AgentRecord(f"{slug}-eng-4", "eng", "t"), AgentRecord(f"{slug}-master-1", "master", "")
    messenger.agent_status(eng), messenger.prompt(eng, "inbox"), messenger.agent_status(master)
    assert calls[0][2] == calls[1][2] != calls[2][2]
    assert calls[0][2].endswith("-eng-4")


def test_inbox_wakes_read_and_prompt_the_agents_pane_id():
    calls = []
    messenger = delivery.HerdrMessenger(herdr=lambda args: calls.append(args) or {"agent_status": "idle"})
    eng = AgentRecord("sw-eng-1", "eng", "t", pane_id="w:p9")
    assert messenger.agent_status(eng) == "idle"
    messenger.prompt(eng, "inbox")
    assert calls == [["agent", "get", "w:p9"], ["agent", "prompt", "w:p9", "[swarm delivery] inbox"]]


def test_typed_input_reads_the_visible_pane_with_its_styles():
    calls = []
    capture = {"text": "─────\n❯\xa0\x1b[2mTry this\x1b[0m half typed\n─────"}
    messenger = delivery.HerdrMessenger(herdr=lambda args: calls.append(args) or capture)
    assert messenger.typed_input(AgentRecord("sw-eng-1", "eng", "t", pane_id="w:p9")) == "half typed"
    assert calls == [["pane", "read", "w:p9", "--source", "visible", "--format", "ansi"]]


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


@pytest.mark.parametrize("fyi", [False, True])
def test_peer_delivery_records_each_receivers_task(store, fyi):
    from scripts.inbox.store import InboxStore

    store.put_agent("sw", AgentRecord(name="sw-eng-2", lane="eng", task="t2"))
    delivery.send(store, "sw", "Tell me when your branch is pushed.", sender="sw-ci-1", to="eng", fyi=fyi)
    inbox = InboxStore(store.redis)
    assert inbox.inbox("sw-eng-1")[0].task == "t"
    assert inbox.inbox("sw-eng-2")[0].task == "t2"
    assert inbox.inbox("sw-eng-1")[0].fyi is fyi
    assert inbox.inbox("sw-eng-2")[0].fyi is fyi


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


def test_a_refused_page_post_closes_that_item_alone_and_tells_its_sender(store):
    from scripts.swarm.store import SwarmError

    class StrictLedger(PageLedger):
        def relay(self, slug, text, by):
            if "18:45" in text:
                raise SwarmError("ledger sw refused: chat refused: clock time '18:45'")
            super().relay(slug, text, by)

    box = InboxStore(store.redis)
    refused = box.send("sw-eng-1", "operator", "the job finished at 18:45")
    shown = box.send("sw-eng-2", "operator", "the docs are merged")
    ledger = StrictLedger()
    assert delivery.relay_to_page(box, "sw", store.agents("sw"), ledger) == 1
    assert ledger.said == [("the docs are merged", "sw-eng-2")]
    closed = box.get(refused.id)
    assert closed.state == "cancelled" and "refused" in closed.reason and "clock time" in closed.reason
    assert box.get(shown.id).state == "done"
    [notice] = box.pending_items("sw-eng-1")
    assert notice.sender == "swarm" and "clock time" in notice.text and refused.id in notice.text


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
