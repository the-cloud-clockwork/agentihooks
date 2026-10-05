from dataclasses import replace
from types import SimpleNamespace

import fakeredis
import pytest

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm.naming import NamingError
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.terminate_agent import Session, resolve

pytestmark = pytest.mark.unit


@pytest.fixture
def store():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/home/x/agentihooks", 2, 1))
    return store


def test_old_address_delivers_existing_and_new_mail_to_the_new_name(store):
    inbox = InboxStore(store.redis)
    old = "sw-eng-1"
    before = inbox.send("operator", old, "before rename")
    new = store.next_name("sw", "eng")
    store.names.alias(old, new)
    after = inbox.send("operator", old, "after rename")
    assert after.address == new
    assert {i.id for i in inbox.pending_mail(new)} == {before.id, after.id}
    assert inbox.deliver(before.id, new).state == "delivered"
    assert inbox.deliver(after.id, old).state == "delivered"
    assert inbox.pending_mail(old) == []
    inbox.reply(before.id, old, "received")
    assert inbox.get(before.id).state == "done"


def test_terminate_finds_the_same_session_by_either_name(store):
    old = "sw-eng-1"
    new = store.next_name("sw", "eng")
    store.names.alias(old, new)
    session = Session("conversation", "codex", old, SimpleNamespace(pid=123), "/repo", "alive")
    for name in (old, new):
        assert resolve([session], name, "any", names=store.names) is session
    renamed = replace(session, name=new)
    assert resolve([renamed], old, "any", names=store.names) is renamed


def test_alias_cannot_be_reassigned_or_chain(store):
    old = "sw-eng-1"
    new = store.next_name("sw", "eng")
    other = store.next_name("sw", "eng")
    store.names.alias(old, new)
    store.names.alias(old, new)
    with pytest.raises(NamingError):
        store.names.alias(old, other)
    store.names.alias("another-old-name", old)
    assert store.names.resolve("another-old-name") == new
    assert store.names.entry(old)["name"] == new
    assert store.names.slug_of(old) == "sw"
    assert set(store.names.aliases(new)) == {old, "another-old-name"}


def test_alias_keeps_seat_authority_for_the_running_old_session(store):
    inbox = InboxStore(store.redis)
    old = "sw-eng-1"
    new = store.next_name("sw", "eng")
    seat = seat_address("sw", "eng-1")
    store.seats.occupy(seat, old, 1)
    store.names.alias(old, new)
    store.seats.occupy(seat, new, 2)
    item = inbox.send("operator", seat, "continue")
    assert inbox.deliver(item.id, old).state == "delivered"
    assert store.seats.seat_of(old) == seat


def test_rename_is_idempotent_and_preserves_claim_seat_and_crew(store):
    from scripts.swarm.rename import rename_swarm
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm_ledger import ledger_names

    old = "sw-eng-1"
    seat = seat_address("sw", "eng-1")
    agent = AgentRecord(old, "eng", "t1", pane_id="w1:p1", seat=seat, conversation_id="conversation")
    store.put_agent("sw", agent)
    store.claim("sw", "t1", old, 30_000)
    store.seats.occupy(seat, old, 1)
    doc = {"tasks": [{"id": "t1", "state": "claimed", "claimed_by": old}], "orchestrator": old}
    meta = {"members": {old: {"claims": ["phases/p7"], "handled_rev": 9}}}
    pane = {"pane_id": "w1:p1", "name": old, "workspace_id": "w1"}
    workspace = {"workspace_id": "w1", "label": "swarm-sw"}
    mutations = []

    def herdr(args):
        if args == ["agent", "list"]:
            return {"agents": [pane]}
        if args == ["workspace", "list"]:
            return {"workspaces": [workspace]}
        mutations.append(args)
        if args[:2] == ["agent", "rename"]:
            pane["name"] = args[3]
        elif args[:2] == ["workspace", "rename"]:
            workspace["label"] = args[3]
        return {}

    def rename_crew(slug, old, new):
        ledger_names.rename(doc, meta, old, new)

    ledger = SimpleNamespace(tasks=lambda slug: doc["tasks"], rename_agent=rename_crew)
    runtime = HerdrRuntime(herdr=herdr)
    actions = rename_swarm(store, "sw", ledger, runtime, 10)
    new = store.agents("sw")[0].name
    assert new != old
    assert pane["name"] == new
    assert workspace["label"] == f"agentihooks-{store.config('sw').code}"
    assert store.claimant("sw", "t1") == new
    assert store.seats.occupant(seat).occupant == new
    assert doc["tasks"][0]["claimed_by"] == new
    assert meta["members"][new]["handled_rev"] == 9
    assert old not in meta["members"]
    assert actions
    first = list(mutations)
    assert rename_swarm(store, "sw", ledger, runtime, 20) == []
    assert mutations == first
    assert len(store.names.names("sw")) == 1


def test_rename_refuses_an_unrelated_pane_before_changing_ownership(store):
    from scripts.swarm.rename import rename_swarm
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import SwarmError

    old = "sw-eng-1"
    store.put_agent("sw", AgentRecord(old, "eng", "t1", pane_id="w1:p1"))
    runtime = HerdrRuntime(herdr=lambda args: {"agents": [{"pane_id": "w1:p1", "name": "somebody-else"}]})
    ledger = SimpleNamespace(tasks=lambda slug: [{"id": "t1", "state": "claimed", "claimed_by": old}])
    with pytest.raises(SwarmError, match="belongs"):
        rename_swarm(store, "sw", ledger, runtime, 10)
    assert store.agents("sw")[0].name == old
    assert store.names.resolve(old) == old


def test_rename_lock_does_not_overlap_the_tick(store):
    from scripts.swarm.rename import rename_swarm
    from scripts.swarm.store import SwarmError

    store.redis.set(store.key("sw", "tick-lock"), "tick", px=30_000)
    with pytest.raises(SwarmError, match="tick"):
        rename_swarm(store, "sw", None, None, 10)


def test_alias_survives_snapshot_restore_without_affecting_another_swarm(store):
    store.create(SwarmConfig("other", "/repo", 1, 0))
    old = "sw-eng-1"
    new = store.next_name("sw", "eng")
    other = store.next_name("other", "eng")
    store.names.alias(old, new)
    store.names.alias("other-eng-1", other)
    snapshot = store.export("sw")
    store.redis.delete(store.names.key("alias", old), store.names.key("aliases-of", new))
    store.restore("sw", snapshot)
    assert store.names.resolve(old) == new
    assert store.names.resolve("other-eng-1") == other


def test_old_name_still_controls_the_renamed_swarm_agent(store):
    from scripts.swarm import cli

    old = "sw-eng-1"
    new = store.next_name("sw", "eng")
    store.names.alias(old, new)
    agent = AgentRecord(new, "eng", "t1")
    store.put_agent("sw", agent)
    assert cli._me(store, SimpleNamespace(name=old, slug="sw")) == agent


def test_ledger_operation_renames_crew_and_old_author_resolves(store, monkeypatch):
    from scripts.swarm.ledger_client import _ledger

    _ledger()
    import ledger_core as core

    old = "sw-eng-1"
    new = store.next_name("sw", "eng")
    store.names.alias(old, new)
    monkeypatch.setattr("hooks._redis.get_redis", lambda: store.redis)
    doc = {"tasks": [{"id": "t1", "claimed_by": old}], "orchestrator": old}
    meta = {"rev": 3, "stamps": {}, "members": {old: {"claims": [], "handled_rev": 1}}}
    ctx = core.Context(meta, 1)
    op = {"op": "agent_rename", "id": "rename", "by": "swarm", "old": old, "new": new}
    core.check_op(op)
    assert core.apply_op(doc, op, ctx)
    assert old not in meta["members"] and new in meta["members"]
    assert core.apply_op(doc, {"op": "ack", "id": "ack", "by": old, "rev": 3}, ctx)
    assert meta["members"][new]["handled_rev"] == 3


def test_rename_failure_reuses_the_reserved_name_on_retry(store):
    from scripts.swarm.rename import rename_swarm
    from scripts.swarm.runtime import HerdrRuntime

    old = "sw-eng-1"
    store.put_agent("sw", AgentRecord(old, "eng", "t1", pane_id="w1:p1"))
    pane = {"pane_id": "w1:p1", "workspace_id": "w1", "name": old}
    fail = [True]

    def herdr(args):
        if args == ["agent", "list"]:
            return {"agents": [pane]}
        if args == ["workspace", "list"]:
            return {"workspaces": []}
        if fail[0]:
            raise RuntimeError("herdr refused the name")
        pane["name"] = args[3]
        return {}

    ledger = SimpleNamespace(rename_agent=lambda *args: None)
    runtime = HerdrRuntime(herdr=herdr)
    with pytest.raises(RuntimeError, match="refused"):
        rename_swarm(store, "sw", ledger, runtime, 10)
    new = store.redis.get(store.key("sw", "rename-to", old))
    assert new and new != old
    assert store.names.resolve(old) == old
    assert store.agents("sw")[0].name == old
    fail[0] = False
    rename_swarm(store, "sw", ledger, runtime, 20)
    assert store.agents("sw")[0].name == new
    assert len(store.names.names("sw")) == 1
