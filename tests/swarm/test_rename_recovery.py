from types import SimpleNamespace

import pytest

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import delivery, idle
from scripts.swarm.rename import _move_agent
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 0))
    return store


def test_a_crash_after_moving_the_record_repairs_the_seat_on_retry(store, monkeypatch):
    old = "sw-eng-1"
    new = store.next_name("sw", "eng")
    seat = seat_address("sw", "eng-1")
    agent = AgentRecord(old, "eng", "t1", seat=seat)
    store.put_agent("sw", agent)
    store.seats.occupy(seat, old, 1)
    store.names.alias(old, new)
    occupy = store.seats.occupy

    def crash(*args):
        raise RuntimeError("crash after record move")

    monkeypatch.setattr(store.seats, "occupy", crash)
    with pytest.raises(RuntimeError, match="crash"):
        _move_agent(store, "sw", agent, new, 2)
    monkeypatch.setattr(store.seats, "occupy", occupy)
    _move_agent(store, "sw", store.agents("sw")[0], new, 3)
    assert store.seats.occupant(seat).occupant == new
    assert store.seats.seat_of(old) == store.seats.seat_of(new) == seat
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", seat, "continue")
    assert inbox.deliver(item.id, old).state == "delivered"


def test_alias_published_during_a_mailbox_read_retries_the_snapshot(store, monkeypatch):
    old = "sw-eng-1"
    new = store.next_name("sw", "eng")
    inbox = InboxStore(store.redis)
    item = inbox.send("operator", new, "new mail")
    original = inbox.names.resolve
    published = []

    def resolve(name, reader=None):
        value = original(name, reader)
        if name == old and not published:
            published.append(True)
            store.names.alias(old, new)
        return value

    monkeypatch.setattr(inbox.names, "resolve", resolve)
    assert [i.id for i in inbox.pending_mail(old)] == [item.id]


def test_existing_replies_and_targeted_messages_use_aliases(store):
    old = "sw-eng-1"
    inbox = InboxStore(store.redis)
    item = inbox.send(old, "operator", "report")
    new = store.next_name("sw", "eng")
    store.names.alias(old, new)
    agent = AgentRecord(new, "eng", "t1")
    store.put_agent("sw", agent)
    assert delivery.recipients(store, "sw", old, "master") == [agent]
    assert delivery.recipients(store, "sw", "all", old) == []
    posted = []
    ledger = SimpleNamespace(relay=lambda slug, text, by: posted.append((text, by)))
    assert delivery.relay_to_page(inbox, "sw", [agent], ledger) == 1
    assert posted == [("report", new)]
    assert inbox.get(item.id).state == "done"


def test_wait_and_heartbeat_survive_rename_and_old_session_publication(store):
    old = "sw-eng-1"
    idle.declare_wait(store.redis, "sw", old, 200, "checks", 1)
    idle.beat(store.redis, "sw", old, "working", 2)
    new = store.next_name("sw", "eng")
    agent = AgentRecord(old, "eng", "t1")
    store.put_agent("sw", agent)
    store.names.alias(old, new)
    _move_agent(store, "sw", agent, new, 3)
    assert idle.wait(store.redis, "sw", new)["until"] == 200
    assert idle.waited(store.redis, "sw", new) == 200
    assert idle.heartbeat(store.redis, "sw", new)["at"] == 2
    idle.beat(store.redis, "sw", old, "working", 4)
    assert idle.heartbeat(store.redis, "sw", new)["at"] == 4
    idle.declare_wait(store.redis, "sw", old, 300, "reply", 5)
    assert idle.wait(store.redis, "sw", new)["until"] == 300


def test_all_swarm_rename_attempts_every_live_swarm_after_one_failure(store, monkeypatch):
    from scripts.swarm import cli, rename
    from scripts.swarm.store import SwarmError

    store.create(SwarmConfig("other", "/repo", 1, 0))
    for slug in ("sw", "other"):
        store.put_agent(slug, AgentRecord(store.next_name(slug, "master"), "master", "master"))
    attempted = []

    def run(store, slug, ledger, runtime, at):
        attempted.append(slug)
        if slug == "other":
            raise SwarmError("name refused")
        return []

    monkeypatch.setattr(rename, "rename_swarm", run)
    with pytest.raises(SwarmError, match="name refused"):
        cli.cmd_rename(store, SimpleNamespace())
    assert attempted == ["other", "sw"]
