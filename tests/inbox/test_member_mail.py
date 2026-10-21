from dataclasses import replace

import pytest

from scripts.inbox import exits, wake
from scripts.inbox.store import InboxStore
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from tests.inbox.test_wake import FakeHerdr, FakeLedger

pytestmark = pytest.mark.unit

SEAT = "eng-1@sw"
MASTER_NAME = "sw-master-1"


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def store(redis):
    from scripts.hive import auth

    store = RedisStore(redis)
    store.create(SwarmConfig("sw", "agentihooks", 1, 0))
    redis.hset(f"{auth.PREFIX}:member:m1", mapping={"name": "m1", "ledger": "digest"})
    redis.sadd(auth.INDEX, "m1")
    return store


@pytest.fixture
def agent(store):
    name = store.next_name("sw", "eng")
    store.start_execution("sw", AgentRecord(name, "eng", "t1", seat=SEAT, hive="m1"))
    store.seats.occupy(SEAT, name, 1)
    return next(a for a in store.agents("sw") if a.name == name)


@pytest.fixture
def mail(redis, agent):
    item = InboxStore(redis).send("sender", agent.name, "contract")
    member = InboxStore(redis, world="m1")
    assert InboxStore(redis, world="").inbox(agent.name) == []
    assert [i.id for i in member.inbox(agent.name)] == [item.id]
    return member, item


def test_the_controller_inbox_reads_every_member_world_with_its_persist_hook(store):
    persist = object()
    assert [(w.world, w.persist) for w in InboxStore(store.redis, persist=persist, world="").worlds()] == [
        ("", persist),
        ("m1", persist),
    ]


def test_a_member_world_inbox_reads_only_itself_on_the_controller(store):
    assert [w.world for w in InboxStore(store.redis, world="m1").worlds()] == ["m1"]


def test_the_wake_pass_escalates_a_member_agents_unread_mail_to_the_controller_master(redis, agent, mail):
    member, item = mail
    agents = [agent, AgentRecord(MASTER_NAME, MASTER, MASTER, pane_id="pm")]
    actions = wake.wake_pass(InboxStore(redis), "sw", agents, FakeHerdr({}), FakeLedger(), item.created_at + 10, 1)
    assert f"raised message {item.id} to {MASTER_NAME}" in actions
    assert wake.TO_MASTER in [e.get("event") for e in member.history(item.id)]
    [raised] = InboxStore(redis, world="").pending_items(MASTER_NAME)
    assert raised.ref == f"inbox-escalation:{item.id}"


def test_the_wake_pass_types_no_wake_into_a_pane_on_another_host(redis, agent, mail):
    member, item = mail
    remote = replace(agent, harness="codex", pane_id="p1", runtime_target={"host_id": "m1"})
    herdr = FakeHerdr({"p1": "idle"})
    wake.wake_pass(InboxStore(redis), "sw", [remote], herdr, FakeLedger(), item.created_at + 1, 300_000)
    assert herdr.prompts == []
    assert wake.WOKEN not in [e.get("event") for e in member.history(item.id)]


def test_the_exit_sweep_moves_a_gone_member_agents_mail_to_its_seat(store, agent, mail):
    member, item = mail
    store.put_agent("sw", replace(agent, state="finished"))
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert member.get(item.id).address == SEAT


def test_the_exit_sweep_withdraws_the_master_escalation_of_a_member_agents_unfillable_seat(store, agent, mail):
    member, item = mail
    agents = [agent, AgentRecord(MASTER_NAME, MASTER, MASTER, pane_id="pm")]
    wake.wake_pass(InboxStore(store.redis), "sw", agents, FakeHerdr({}), FakeLedger(), item.created_at + 10, 1)
    [raised] = InboxStore(store.redis, world="").pending_items(MASTER_NAME)
    store.put_agent("sw", replace(agent, state="finished"))
    store.update("sw", state="stopped")
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert member.get(item.id).state == "cancelled"
    assert InboxStore(store.redis, world="").get(raised.id).state == "cancelled"


class Live:
    def __init__(self, *names):
        self.names = set(names)

    def live_names(self):
        return self.names


def test_swarm_status_lists_a_member_agents_pending_mail(store, agent, mail, monkeypatch):
    from scripts.swarm import status

    monkeypatch.setattr(status, "page_quota", lambda: {})
    report = status.status_report(store, "sw", {"tasks": []}, Live(agent.name))
    [row] = [a for a in report["agents"] if a["name"] == agent.name]
    assert [m["text"] for m in row["inbox"]] == ["contract"]


def test_a_handoff_moves_a_member_agents_mail_to_its_seat(redis, agent, mail):
    member, item = mail
    exits.settle(InboxStore(redis), agent.name, SEAT, "handed off its seat")
    assert member.get(item.id).address == SEAT


def test_a_handoff_closes_a_member_agents_wait_ended_notice(redis, agent):
    from scripts.swarm import waits

    notice = InboxStore(redis).send(waits.SENDER, SEAT, f"Your wait on checks has ended.{waits._pick_up('t1')} go on")
    member = InboxStore(redis, world="m1")
    assert member.get(notice.id).state == "pending"
    waits.settle_notices(InboxStore(redis), agent, "a handoff")
    assert member.get(notice.id).state == "done"


def test_removing_a_swarm_withdraws_its_member_agents_open_mail(redis, agent, mail):
    member, item = mail
    exits.close_swarm(InboxStore(redis), "sw")
    assert member.get(item.id).state == "cancelled"
