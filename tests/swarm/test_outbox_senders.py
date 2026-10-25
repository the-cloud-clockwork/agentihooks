from argparse import Namespace

import pytest

from scripts.inbox import outbox
from scripts.inbox.seen import write_ref
from scripts.inbox.store import InboxStore
from scripts.swarm import control_notifications, delivery, operator_mail
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig, Unreachable
from scripts.swarm_ledger import ledger_alerts
from scripts.swarm_ledger.watch_ledger import line

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
    assert items(inbox, ENG.name) == [] and outbox.waiting()
    outbox.flush(inbox)
    outbox.flush(inbox)
    [item] = items(inbox, ENG.name)
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
    assert 0 < inbox.redis.ttl(inbox.key("ledger-sent", write_ref(SLUG, event))) <= operator_mail.SENT_TTL_S
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
    assert len(ledger_alerts.deliver(inbox, SLUG, [alert(2), alert(3), alert(4)], 4, BOSS.seat, since=3)) == 2
    assert sorted(i.text.split("alert ")[1].split(" ")[0] for i in items(inbox, BOSS.seat)) == ["al-3-0", "al-4-0"]


def unreachable():
    raise Unreachable("Redis is unreachable (connection refused); the swarm refuses to run without it")


def test_swarm_say_while_redis_is_fully_down_reaches_its_recipient_once_after_recovery(swarm, monkeypatch):
    from scripts.swarm import cli

    _, inbox = swarm
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", BOSS.name)
    monkeypatch.setattr(cli, "connect", unreachable)
    assert cli.main([SLUG, "say", "the docs build is green", "--to", "eng", "--fyi"]) == 0
    assert items(inbox, ENG.name) == [] and outbox.waiting()
    outbox.flush(inbox)
    outbox.flush(inbox)
    [item] = items(inbox, ENG.name)
    assert (item.sender, item.text, item.task, item.fyi) == (BOSS.name, "the docs build is green", ENG.task, True)
    assert not outbox.waiting()


@pytest.mark.parametrize("to", [["--to", "operator"], []])
def test_a_say_to_the_operator_while_redis_is_down_is_refused_not_queued(swarm, monkeypatch, capsys, to):
    from scripts.swarm import cli

    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", BOSS.name)
    monkeypatch.setattr(cli, "connect", unreachable)
    assert cli.main([SLUG, "say", "for the page", *to]) == 1
    assert "swarm: Redis is unreachable" in capsys.readouterr().err and not outbox.waiting()


def test_a_say_naming_nobody_while_redis_is_up_is_refused_not_queued(swarm, monkeypatch, capsys):
    from scripts.swarm import cli

    store, _ = swarm
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", BOSS.name)
    monkeypatch.setattr(cli, "connect", lambda: store)
    assert cli.main([SLUG, "say", "anyone", "--to", "ghost"]) == 1
    assert "swarm: nobody in swarm" in capsys.readouterr().err and not outbox.waiting()


def test_a_redis_error_outside_a_say_to_agents_is_raised(swarm, monkeypatch):
    import redis

    from scripts.swarm import cli

    store, _ = swarm

    def lost(slug):
        raise redis.ConnectionError("connection lost")

    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", BOSS.name)
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(store, "agents", lost)
    with pytest.raises(redis.ConnectionError):
        cli.main([SLUG, "say", "for the page", "--to", "operator"])
    assert not outbox.waiting()


def test_a_queued_say_to_everyone_reaches_each_recipient_once(swarm):
    _, inbox = swarm
    delivery.queue(SLUG, "the docs build is green", "sw-ci-9", "all")
    outbox.flush(inbox)
    assert [len(items(inbox, agent.name)) for agent in (ENG, BOSS)] == [1, 1]


def test_a_queued_swarm_say_naming_nobody_is_set_aside_and_the_next_records_still_land(swarm):
    _, inbox = swarm
    delivery.queue(SLUG, "anyone there", BOSS.name, "ci")
    delivery.queue(SLUG, "the docs build is green", BOSS.name, ENG.name)
    delivery.queue(SLUG, "and rebase onto dev", BOSS.name, ENG.name)
    report = outbox.flush(inbox)
    [(_, reason)] = report.rejected
    assert "nobody in swarm" in reason and not outbox.waiting()
    found = items(inbox, ENG.name)
    assert [(i.text, i.fyi) for i in found] == [("the docs build is green", False), ("and rebase onto dev", False)]


def test_a_say_record_applied_twice_writes_each_recipient_once(swarm):
    from scripts.inbox import cli as inbox_cli

    _, inbox = swarm
    record = outbox.record("say", BOSS.name, {"slug": SLUG, "to": ENG.name, "text": "rebase first", "fyi": False})
    assert inbox_cli.apply(inbox, record) == [ENG.name]
    assert inbox_cli.apply(inbox, record) == [ENG.name]
    assert [i.text for i in items(inbox, ENG.name)] == ["rebase first"]


def test_an_alert_without_a_revision_is_never_delivered(swarm):
    _, inbox = swarm
    bare = {key: value for key, value in alert(3).items() if key != "rev"}
    assert ledger_alerts.deliver(inbox, SLUG, [bare], 4, BOSS.seat, since=0) == []
    assert items(inbox, BOSS.seat) == []


def test_an_alert_delivered_again_after_its_mark_expired_is_one_item_with_its_stable_id(swarm):
    _, inbox = swarm
    ledger_alerts.deliver(inbox, SLUG, [alert(3)], 3, BOSS.seat)
    inbox.redis.delete(inbox.key("alert-sent", SLUG, "al-3-0"))
    ledger_alerts.deliver(inbox, SLUG, [alert(3)], 3, BOSS.seat)
    [item] = items(inbox, BOSS.seat)
    assert item.id == outbox.stable_id("alert", SLUG, "al-3-0") and len(item.id) == 12


def test_a_stable_id_is_the_short_hash_of_its_parts_one_per_line():
    import hashlib

    assert outbox.stable_id("ledger:sw:5", "eng-1@sw") == hashlib.sha256(b"ledger:sw:5\neng-1@sw").hexdigest()[:12]
