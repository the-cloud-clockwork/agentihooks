from argparse import Namespace

import pytest

from scripts.inbox import outbox
from scripts.inbox.seen import write_ref
from scripts.inbox.store import InboxStore
from scripts.swarm import control_notifications, delivery, operator_mail
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_alerts
from scripts.swarm_ledger.watch_ledger import line

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "sw"
ENG = AgentRecord("sw-eng-1", "eng", "t1", pane_id="p1", seat=f"eng-1@{SLUG}")
BOSS = AgentRecord("sw-master-1", MASTER, MASTER, pane_id="pm", seat=f"master@{SLUG}")


@pytest.fixture
def swarm(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 1, 1))
    for agent in (ENG, BOSS):
        store.put_agent(SLUG, agent)
        store.seats.occupy(agent.seat, agent.name, 1)
    return store, InboxStore(store.redis)


@pytest.fixture
def lost(monkeypatch):
    """The next n inbox writes lose their Redis connection before they land."""
    import redis

    original = InboxStore.send
    left = []

    def send(self, *args, **kwargs):
        if left:
            left.pop()
            raise redis.ConnectionError("connection lost before the send landed")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(InboxStore, "send", send)
    return lambda n=1: left.extend([1] * n)


def items(inbox, address):
    return inbox.inbox(address)


def test_swarm_say_queues_a_send_redis_dropped_and_replays_it_once(swarm, lost):
    store, inbox = swarm
    lost()
    assert delivery.send(store, SLUG, "the docs build is green", sender=BOSS.name, to=ENG.name) == [ENG.name]
    assert items(inbox, ENG.seat) == [] and outbox.waiting()
    outbox.flush(inbox)
    outbox.flush(inbox)
    [item] = items(inbox, ENG.seat)
    assert (item.sender, item.text, item.task) == (BOSS.name, "the docs build is green", ENG.task)


def test_a_control_notice_redis_dropped_reaches_the_master_once_after_replay(swarm, lost, monkeypatch):
    store, inbox = swarm
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    lost()
    args = Namespace(slug=SLUG, name="", now=False)
    control_notifications.notify(store, args, ledger=None, before=None, action="pause")
    assert items(inbox, BOSS.seat) == []
    outbox.flush(inbox)
    outbox.flush(inbox)
    [item] = items(inbox, BOSS.seat)
    assert item.fyi and item.ref.startswith(control_notifications.CONTROL_REF) and "paused the swarm" in item.text


def write(rev, target="phases/p1", text="please look"):
    return {
        "rev": rev,
        "at": rev,
        "by": "operator",
        "kind": "comment added",
        "target": target,
        "id": f"c-{rev}",
        "text": text,
    }


def test_operator_mail_queued_on_a_dropped_send_is_marked_sent_and_replays_once(swarm, lost):
    store, inbox = swarm
    event = write(5)
    lost()
    operator_mail.relay(inbox, store, SLUG, {}, [event], line)
    assert inbox.redis.exists(inbox.key("ledger-sent", write_ref(SLUG, event)))
    operator_mail.relay(inbox, store, SLUG, {}, [event], line)
    outbox.flush(inbox)
    outbox.flush(inbox)
    assert len(items(inbox, BOSS.seat)) == 1


def test_operator_mail_that_could_neither_send_nor_queue_is_not_marked_sent(swarm, lost, monkeypatch):
    store, inbox = swarm
    event = write(5)
    monkeypatch.setenv(outbox.MAX_ENV, "10")
    lost()
    with pytest.raises(outbox.OutboxFull):
        operator_mail.relay(inbox, store, SLUG, {}, [event], line)
    assert not inbox.redis.exists(inbox.key("ledger-sent", write_ref(SLUG, event)))
    operator_mail.relay(inbox, store, SLUG, {}, [event], line)
    assert len(items(inbox, BOSS.seat)) == 1


def test_operator_mail_sent_twice_by_two_relays_is_one_item(swarm):
    store, inbox = swarm
    event = write(5)
    operator_mail.relay(inbox, store, SLUG, {}, [event], line)
    inbox.redis.delete(inbox.key("ledger-sent", write_ref(SLUG, event)))
    operator_mail.relay(inbox, store, SLUG, {}, [event], line)
    assert len(items(inbox, BOSS.seat)) == 1


def alert(rev):
    return {
        "id": f"al-{rev}-0",
        "rev": rev,
        "state": ledger_alerts.OPEN,
        "source": "size",
        "target": "master",
        "text": "x",
    }


def test_an_alert_that_could_neither_send_nor_queue_is_not_marked_sent(swarm, lost, monkeypatch):
    _, inbox = swarm
    monkeypatch.setenv(outbox.MAX_ENV, "10")
    lost()
    with pytest.raises(outbox.OutboxFull):
        ledger_alerts.deliver(inbox, SLUG, [alert(3)], 3, BOSS.seat)
    ledger_alerts.deliver(inbox, SLUG, [alert(3)], 3, BOSS.seat)
    ledger_alerts.deliver(inbox, SLUG, [alert(3)], 3, BOSS.seat)
    assert len(items(inbox, BOSS.seat)) == 1


def test_alerts_since_an_older_revision_are_all_delivered(swarm):
    _, inbox = swarm
    ledger_alerts.deliver(inbox, SLUG, [alert(2), alert(3), alert(4)], 4, BOSS.seat, since=3)
    assert sorted(i.text.split("alert ")[1].split(" ")[0] for i in items(inbox, BOSS.seat)) == ["al-3-0", "al-4-0"]
