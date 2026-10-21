import json

import pytest

from scripts.swarm.keyspace import ROOT

pytestmark = pytest.mark.unit

MEMBER_URL = "rediss://hive.example:6380/0"
COMPOSE = {"AGENTIHOOKS_DEPLOYMENT": "compose"}


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def swarm(redis):
    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(redis)
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    return store


def place(store, hive, seat="eng-1@fixture"):
    from scripts.swarm.store import AgentRecord

    name = store.next_name("fixture", "eng")
    store.start_execution("fixture", AgentRecord(name, "eng", "task", seat=seat, hive=hive))
    store.seats.occupy(seat, name, 1)
    return name


def join(redis, member):
    from scripts.hive import auth

    redis.hset(f"{auth.PREFIX}:member:{member}", mapping={"name": member, "ledger": "digest"})
    redis.sadd(auth.INDEX, member)


def as_member(monkeypatch, member="m1"):
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "compose")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", member)
    monkeypatch.setenv("AGENTIHOOKS_HIVE_REDIS_URL", MEMBER_URL)


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({**COMPOSE, "AGENTIHOOKS_HIVE_ID": "m1", "AGENTIHOOKS_HIVE_REDIS_URL": MEMBER_URL}, "m1"),
        (
            {"AGENTIHOOKS_DEPLOYMENT": "local", "AGENTIHOOKS_HIVE_ID": "m1", "AGENTIHOOKS_HIVE_REDIS_URL": MEMBER_URL},
            "",
        ),
        ({**COMPOSE, "AGENTIHOOKS_HIVE_REDIS_URL": MEMBER_URL}, ""),
        ({**COMPOSE, "AGENTIHOOKS_HIVE_ID": "m1"}, ""),
    ],
)
def test_a_process_is_a_member_only_while_it_talks_redis_as_that_member(environ, expected):
    from scripts.hive import scope

    assert scope.member(environ) == expected


def test_a_member_beat_lands_below_its_own_hive_key_and_the_controller_reads_it_there(redis, swarm, monkeypatch):
    from scripts.swarm import idle

    name = place(swarm, "m1")
    join(redis, "m1")
    with monkeypatch.context() as member:
        as_member(member)
        idle.beat(redis, "fixture", name, "working", 5)
        idle.prompted(redis, "fixture", name, 6)
        idle.declare_wait(redis, "fixture", name, 100, "checks", 7)
        assert idle.heartbeat(redis, "fixture", name) == {"state": "working", "at": 5}

    assert redis.ttl(f"{ROOT}:hive:m1:swarm:fixture:heartbeat:{name}") == idle.BEAT_TTL_S
    assert not redis.keys(f"{ROOT}:swarm:fixture:*:{name}")
    assert idle.heartbeat(redis, "fixture", name) == {"state": "working", "at": 5}
    assert idle.last_prompt(redis, "fixture", name) == 6
    assert idle.wait(redis, "fixture", name)["reason"] == "checks"
    assert idle.waited(redis, "fixture", name) == 100
    assert [entry["at"] for entry in idle.wait_history(redis, "fixture", name)] == [7]
    idle.end_wait(redis, "fixture", name, 60)
    assert idle.wait(redis, "fixture", name) is None
    assert idle.waited(redis, "fixture", name) == 60


def test_the_controller_records_a_member_agents_beat_prompt_and_wait_below_its_member(redis, swarm):
    from scripts.swarm import idle

    name = place(swarm, "m1")
    join(redis, "m1")
    idle.beat(redis, "fixture", name, "idle", 5)
    idle.prompted(redis, "fixture", name, 6)
    idle.declare_wait(redis, "fixture", name, 100, "checks", 7)

    own = f"{ROOT}:hive:m1:swarm:fixture"
    assert json.loads(redis.get(f"{own}:heartbeat:{name}")) == {"state": "idle", "at": 5}
    assert redis.get(f"{own}:prompt:{name}") == "6"
    assert json.loads(redis.get(f"{own}:wait:{name}"))["reason"] == "checks"
    assert not redis.keys(f"{ROOT}:swarm:fixture:*:{name}")


def test_a_member_knows_its_own_agents_without_reading_member_records(redis, swarm, monkeypatch):
    from scripts.hive import scope

    mine, other = place(swarm, "m1"), place(swarm, "m2", seat="eng-2@fixture")
    as_member(monkeypatch)
    monkeypatch.setattr(scope, "members", lambda client: pytest.fail("a member read the member records"))

    assert scope.host(redis, "fixture", mine) == "m1"
    assert scope.host(redis, "fixture", other) == ""


def test_the_controller_ends_a_member_agents_wait_below_its_member(redis, swarm, monkeypatch):
    from scripts.swarm import idle, waits

    name = place(swarm, "m1")
    join(redis, "m1")
    with monkeypatch.context() as member:
        as_member(member)
        idle.declare_wait(redis, "fixture", name, 100, "checks", 7)
    own = f"{ROOT}:hive:m1:swarm:fixture"
    previous = redis.get(f"{own}:wait:{name}")

    assert waits._save_wait(redis, "fixture", name, previous, {}, "merged", 50)
    assert not redis.exists(f"{own}:wait:{name}")
    assert redis.get(f"{own}:woke:{name}") == "50"
    assert idle.woke(redis, "fixture", name) == 50
    assert not redis.keys(f"{ROOT}:swarm:fixture:*:{name}")


def test_members_checks_every_member_record_in_one_round_trip(redis, monkeypatch):
    from unittest.mock import Mock

    from scripts.hive import auth, scope

    join(redis, "m2")
    join(redis, "m1")
    redis.sadd(auth.INDEX, "gone")
    direct = Mock(wraps=redis.exists)
    pipeline = Mock(wraps=redis.pipeline)
    monkeypatch.setattr(redis, "exists", direct)
    monkeypatch.setattr(redis, "pipeline", pipeline)

    assert scope.members(redis) == ["m1", "m2"]
    direct.assert_not_called()
    pipeline.assert_called_once_with(transaction=False)


def test_members_of_an_empty_index_opens_no_pipeline(redis, monkeypatch):
    from scripts.hive import scope

    monkeypatch.setattr(redis, "pipeline", lambda **_: pytest.fail("an empty index opened a pipeline"))

    assert scope.members(redis) == []


def relaunch(redis, store, name, hive, generation):
    key = store.key("fixture", "executions")
    row = next(json.loads(raw) for raw in redis.hvals(key) if json.loads(raw)["name"] == name)
    row.update(execution_id=f"exe-{hive}-{generation}", generation=generation, hive=hive)
    redis.hset(key, row["execution_id"], json.dumps(row))


def test_host_reads_one_agents_latest_placement_without_the_agent_list(redis, swarm, monkeypatch):
    from scripts.hive import scope
    from scripts.swarm.store import RedisStore

    join(redis, "m1")
    join(redis, "m2")
    first, second = place(swarm, "m1"), place(swarm, "m2", seat="eng-2@fixture")
    relaunch(redis, swarm, first, "m2", 7)
    relaunch(redis, swarm, second, "m1", 0)
    redis.hdel(swarm.key("fixture", "agents"), second)
    monkeypatch.setattr(RedisStore, "agents", lambda *_: pytest.fail("host read every agent of the swarm"))

    assert scope.host(redis, "fixture", first) == "m2"
    assert scope.host(redis, "fixture", second) == ""
    assert scope.host(redis, "fixture", "nobody") == ""


def test_no_agent_has_a_member_host_while_no_member_has_joined(redis, swarm):
    from scripts.hive import scope

    assert scope.host(redis, "fixture", place(swarm, "m1")) == ""


@pytest.mark.parametrize("hive", ["", "other"])
def test_an_agent_on_the_controller_or_an_unjoined_hive_keeps_the_shared_readings(redis, swarm, hive):
    from scripts.swarm import idle

    join(redis, "m1")
    name = place(swarm, hive)
    idle.beat(redis, "fixture", name, "idle", 9)

    assert json.loads(redis.get(f"{ROOT}:swarm:fixture:heartbeat:{name}")) == {"state": "idle", "at": 9}
    assert idle.heartbeat(redis, "fixture", name) == {"state": "idle", "at": 9}


def test_member_session_model_progress_and_stop_blocks_stay_below_the_member(redis, swarm, monkeypatch):
    from scripts.gates.claim_stop import Streak
    from scripts.gates.progress import Progress
    from scripts.swarm import session_model

    name = place(swarm, "m1")
    join(redis, "m1")
    with monkeypatch.context() as member:
        as_member(member)
        session_model.put(redis, "fixture", name, "opus", "high", 3)
        Progress(redis, "fixture").outcome(name, "pushed", 4)
        assert Streak(swarm, "fixture", name).bump(5) == 1

    own = f"{ROOT}:hive:m1:swarm:fixture"
    assert sorted(redis.keys(f"{own}:*")) == sorted(
        f"{own}:{kind}:{name}" for kind in ("session-model", "progress", "stop-blocks")
    )
    assert session_model.get(redis, "fixture", name)["model"] == "opus"
    assert Progress(redis, "fixture").read(name).outcome == "pushed"
    assert Streak(swarm, "fixture", name).key == f"{own}:stop-blocks:{name}"


def test_a_member_inbox_keeps_its_items_owners_and_marks_below_its_own_hive_key(redis, swarm, monkeypatch):
    from scripts.inbox.dispatch import Dispatcher
    from scripts.inbox.seen import SeenMarks
    from scripts.inbox.store import InboxStore

    name = place(swarm, "m1")
    join(redis, "m1")
    InboxStore(redis).send("operator", name, "hello")
    assert not redis.keys(f"{ROOT}:inbox:*")
    with monkeypatch.context() as member:
        as_member(member)
        inbox = InboxStore(redis)
        item = inbox.pending_mail(name)[0]
        inbox.read(item.id, name)
        assert SeenMarks(redis).mark(name, "fixture:1:note")
        Dispatcher(inbox).own(name, "session")

    assert not redis.keys(f"{ROOT}:inbox:*")
    assert redis.hget(f"{ROOT}:hive:m1:inbox:item:{item.id}", "state") == "read"
    assert redis.exists(f"{ROOT}:hive:m1:inbox:seen:{name}")
    assert redis.get(f"{ROOT}:hive:m1:inbox:owner:{name}") == "session"


def test_a_member_send_waits_in_its_own_keys_until_the_controller_relays_it(redis, swarm, monkeypatch):
    from scripts.hive import scope
    from scripts.inbox import relay
    from scripts.inbox.store import InboxStore

    name = place(swarm, "m1")
    join(redis, "m1")
    with monkeypatch.context() as member:
        as_member(member)
        inbox = InboxStore(redis)
        forged = inbox.send("operator", "master@fixture", "forged")
        own = inbox.send("operator", name, "for my own agent")
        item = inbox.send(name, "master@fixture", "done", ref="fixture:7:note", fyi=True, task="t1")
        assert not redis.keys(f"{ROOT}:inbox:*")

    with monkeypatch.context() as controller:
        controller.setattr(scope, "member", lambda environ=None: "elsewhere")
        controller.setattr(scope, "joined", lambda client: ["m1"])
        assert relay.relay(redis) == [item.id]
    relayed = InboxStore(redis).get(item.id)
    assert [mail.id for mail in InboxStore(redis).open_items("master@fixture")] == [item.id]
    assert (relayed.sender, relayed.ref, relayed.fyi, relayed.task) == (name, "fixture:7:note", True, "t1")
    world = InboxStore(redis, world="m1")
    assert (world.get(item.id).state, world.get(item.id).reason) == ("cancelled", relay.RELAYED)
    assert world.get(forged.id).reason == relay.FOREIGN_SENDER
    assert [entry["by"] for entry in world.history(item.id)][-1] == relay.RELAYER
    assert [entry["by"] for entry in world.history(forged.id)][-1] == relay.RELAYER
    assert world.get(own.id).state == "pending"
    assert world.pending_items("master@fixture") == []
    assert relay.relay(redis) == []


def test_a_controller_send_to_an_agent_on_a_member_lands_in_that_members_keys(redis, swarm, monkeypatch):
    from scripts.inbox.store import InboxStore

    name = place(swarm, "m1")
    join(redis, "m1")
    persisted = []
    inbox = InboxStore(redis, persist=persisted.append)
    item = inbox.send(
        "master@fixture", "eng-1@fixture", "next step", ref="fixture:3:note", fyi=True, task="t2", item_id="given"
    )

    assert item.id == "given"
    assert len(persisted) == 1
    assert not redis.exists(f"{ROOT}:inbox:item:{item.id}")
    stored = InboxStore(redis, world="m1").get("given")
    assert (stored.sender, stored.address, stored.ref, stored.fyi, stored.task) == (
        "master@fixture",
        "eng-1@fixture",
        "fixture:3:note",
        True,
        "t2",
    )
    with monkeypatch.context() as member:
        as_member(member)
        assert [mail.id for mail in InboxStore(redis).pending_mail(name)] == [item.id]


def test_the_controller_refreshes_each_live_hives_own_launch_slots_across_two_ticks(redis, swarm, monkeypatch):
    from scripts.hive import registry
    from scripts.routing.reservation import LaunchSlots
    from scripts.swarm.store import AgentRecord

    monkeypatch.setattr("time.time", lambda: 100.0)
    monkeypatch.setenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", "50")
    slots = LaunchSlots(redis)
    executions = {}
    for index, (label, hive) in enumerate((("live", "m1"), ("stale", "gone"), ("other", "m2"), ("unreported", "m1"))):
        name = swarm.next_name("fixture", "eng")
        placed = AgentRecord(name, "eng", "task", seat=f"eng-{index + 1}@fixture", hive=hive)
        executions[label] = swarm.start_execution("fixture", placed).execution_id
        assert slots.reserve("claude:one", executions[label], 4, 10000)
        assert slots.acknowledge(executions[label])
        redis.pexpire(f"{ROOT}:launch-slot:{executions[label]}", 1000)
    reports = (("m1", 100_000, ["live", "other"]), ("gone", 1, ["stale"]), ("m2", 100_000, []))
    for hive, at, labels in reports:
        redis.sadd(registry.INDEX, hive)
        redis.hset(registry.key(hive), "heartbeat_at", at)
        reported = {"_executions": {"claude:one": [executions[label] for label in labels]}}
        redis.set(f"{registry.key(hive)}:sessions", json.dumps(reported))

    slots.refresh_reported(100_000)

    ttls = {
        label: round(redis.pttl(f"{ROOT}:launch-slot:{execution}") / 1000) for label, execution in executions.items()
    }
    assert ttls == {"live": 100, "stale": 1, "other": 1, "unreported": 1}


def test_a_member_inbox_pools_and_owns_below_its_own_keys(redis, swarm):
    from scripts.inbox.dispatch import Dispatcher
    from scripts.inbox.store import InboxStore, owner_key

    world = InboxStore(redis, world="m1")
    Dispatcher(world).own("engineer", "session")

    assert world.pool.key("fixture", "turn") == f"{ROOT}:hive:m1:inbox:pool:fixture:turn"
    assert InboxStore(redis, world="").pool.key("fixture") == f"{ROOT}:inbox:pool:fixture"
    assert owner_key("engineer", "m1") == f"{ROOT}:hive:m1:inbox:owner:engineer"
    assert redis.get(f"{ROOT}:hive:m1:inbox:owner:engineer") == "session"
    assert not redis.exists(owner_key("engineer", ""))


def test_a_controller_delivers_a_member_inbox_through_that_members_owner_key(redis, swarm):
    from scripts.inbox import receipts
    from scripts.inbox.dispatch import Dispatcher
    from scripts.inbox.store import InboxStore

    name = place(swarm, "m1")
    join(redis, "m1")
    world = InboxStore(redis, world="m1")
    sent = world.send("operator", name, "hello")
    dispatcher = Dispatcher(world)
    owned = f"{ROOT}:hive:m1:inbox:owner:{name}"

    dispatcher.own(name, "pump", ttl_s=5)
    assert dispatcher.owner(name) == "pump"
    assert world.deliver(sent.id, name) is None
    assert dispatcher.renew(name, "pump", ttl_s=50)
    assert redis.ttl(owned) == 50
    [delivery] = dispatcher.reserve(name, "pump")
    assert [recovered.state for recovered in dispatcher.receipts.recover(name, "pump")] == ["rejected"]
    assert (dispatcher.receipts.get(delivery.id).state, dispatcher.receipts.get(delivery.id).reason) == (
        "rejected",
        receipts.RELEASED,
    )
    assert dispatcher.release(name, "pump")
    assert not redis.exists(owned)
    assert not redis.keys(f"{ROOT}:inbox:*")


@pytest.mark.parametrize(("hive", "pooled"), [("m1", []), ("", ["master@fixture", "master-2@fixture"])])
def test_the_master_pool_holds_only_masters_whose_inboxes_share_its_world(redis, swarm, monkeypatch, hive, pooled):
    from scripts.inbox import pool
    from scripts.inbox.store import InboxStore

    join(redis, "m1")
    place(swarm, hive, seat="master-2@fixture")
    monkeypatch.setattr(pool, "live_masters", lambda client, slug: ["master@fixture", "master-2@fixture"])

    assert InboxStore(redis, world="").pool.masters_for("engineer@0001-0001", "master@fixture") == pooled
