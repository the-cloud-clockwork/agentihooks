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


def test_the_wake_pass_escalates_a_member_agents_unread_mail_to_the_controller_master(redis, agent, mail):
    member, item = mail
    agents = [agent, AgentRecord(MASTER_NAME, MASTER, MASTER, pane_id="pm")]
    actions = wake.wake_pass(InboxStore(redis), "sw", agents, FakeHerdr({}), FakeLedger(), item.created_at + 10, 1)
    assert f"raised message {item.id} to {MASTER_NAME}" in actions
    assert wake.TO_MASTER in [e.get("event") for e in member.history(item.id)]
    [raised] = InboxStore(redis, world="").pending_items(MASTER_NAME)
    assert raised.ref == f"inbox-escalation:{item.id}"


def test_the_exit_sweep_moves_a_gone_member_agents_mail_to_its_seat(store, agent, mail):
    member, item = mail
    store.put_agent("sw", replace(agent, state="finished"))
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert member.get(item.id).address == SEAT


def test_swarm_status_lists_a_member_agents_pending_mail(store, agent, mail):
    from scripts.swarm.status import status_report

    report = status_report(store, "sw", {"tasks": []})
    [row] = [a for a in report["agents"] if a["name"] == agent.name]
    assert [m["text"] for m in row["inbox"]] == ["contract"]


def test_a_handoff_moves_a_member_agents_mail_to_its_seat(redis, agent, mail):
    member, item = mail
    exits.settle(InboxStore(redis), agent.name, SEAT, "handed off its seat")
    assert member.get(item.id).address == SEAT


def test_removing_a_swarm_withdraws_its_member_agents_open_mail(redis, agent, mail):
    member, item = mail
    exits.close_swarm(InboxStore(redis), "sw")
    assert member.get(item.id).state == "cancelled"
