import json

import pytest

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import cli, naming
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/home/x/dev/agentihooks", 2, 1))
    return store


def test_the_swarm_name_is_swarm_at_its_code():
    assert naming.swarm_name("a1b2c3") == "swarm@a1b2c3"
    assert naming.swarm_code("swarm@a1b2c3") == "a1b2c3"
    assert naming.swarm_code("swarm@a1b2c") == ""
    assert naming.swarm_code("engineer@a1b2c3-0001") == ""
    assert naming.swarm_code("rig-grade-swarm") == ""


def test_creating_a_swarm_registers_it_as_swarm_at_its_code(store):
    config = store.config("sw")
    assert config.name == f"swarm@{config.code}"
    assert store.names.swarm(config.code) == {
        "swarm": "sw",
        "name": config.name,
        "ledger": "sw",
        "repo": "/home/x/dev/agentihooks",
    }


def test_the_slug_or_the_name_resolves_the_same_swarm(store):
    name = store.config("sw").name
    assert store.resolve("sw") == "sw"
    assert store.resolve(name) == "sw"
    assert store.resolve("swarm@ffffff") == "swarm@ffffff"
    with pytest.raises(SwarmError, match="no swarm swarm@ffffff"):
        store.config(store.resolve("swarm@ffffff"))


def test_a_removed_swarm_name_no_longer_resolves(store):
    name = store.config("sw").name
    store.remove("sw")
    assert store.resolve(name) == name


def test_the_name_never_changes_after_creation(store):
    name = store.config("sw").name
    with pytest.raises(SwarmError) as refused:
        store.update("sw", name="swarm@ffffff")
    assert str(refused.value) == f"swarm {name} keeps its name: a swarm name never changes after creation"
    assert store.config("sw").name == name
    assert store.update("sw", state="paused").name == f"swarm@{store.config('sw').code}"


def test_rename_migrates_an_existing_swarm_keeping_agents_claims_and_seats(env, monkeypatch):  # noqa: F811
    from scripts.swarm.runtime import HerdrRuntime, herdr_target

    store, _, _ = env
    store.create(SwarmConfig("old", "/repo", 1, 0))
    agent = store.next_name("old", "eng")
    seat = seat_address("old", "eng-1")
    store.put_agent("old", AgentRecord(agent, "eng", "t1", pane_id="w1:p1", seat=seat))
    pane = {"pane_id": "w1:p1", "name": herdr_target(agent), "workspace_id": "w1"}
    listed = {("agent", "list"): {"agents": [pane]}, ("workspace", "list"): {"workspaces": []}}
    monkeypatch.setattr(cli, "HerdrRuntime", lambda: HerdrRuntime(herdr=lambda args: listed[tuple(args)]))
    store.claim("old", "t1", agent, 30_000)
    store.seats.occupy(seat, agent, 1)
    item = InboxStore(store.redis).send("operator", seat, "carry on")
    store.redis.hdel(store.key("old", "config"), "name")
    assert store.config("old").name == ""
    assert run("rename") == 0
    config = store.config("old")
    assert config.name == f"swarm@{config.code}"
    assert [a.name for a in store.agents("old")] == [agent]
    assert store.claimant("old", "t1") == agent
    assert store.seats.occupant(seat).occupant == agent
    assert store.resolve(config.name) == "old"
    assert [i.id for i in InboxStore(store.redis).pending_mail(agent)] == [item.id]


def test_status_list_and_names_show_the_swarm_name(env, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    name = store.config("sw").name
    capsys.readouterr()
    run("sw", "status")
    assert capsys.readouterr().out.splitlines()[0].startswith(f"{name}  sw  paused")
    run("sw", "status", "--json")
    assert json.loads(capsys.readouterr().out)["config"]["name"] == name
    store.create(SwarmConfig("old", "/repo", 1, 0))
    store.redis.hdel(store.key("old", "config"), "name")
    run("list")
    assert capsys.readouterr().out.splitlines() == [
        "-\told\trunning\teng 1\tci 0\tplan 1\tagents 0\t/repo",
        f"{name}\tsw\tpaused\teng 2\tci 1\tplan 1\tagents 0\t/repo",
    ]
    run("sw", "names")
    assert capsys.readouterr().out.startswith(f"name {name}\tcode ")


def test_a_command_given_the_name_acts_on_the_swarm(env, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    name = store.config("sw").name
    assert run(name, "pause") == 0
    assert store.config("sw").state == "paused"
    capsys.readouterr()
    assert run(name, "status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["config"]["slug"] == "sw"


def test_a_name_cannot_create_a_swarm(env, capsys):  # noqa: F811
    assert run("swarm@a1b2c3", "create", "--repo", "/repo") == 1
    assert cli.SLUG_RE.match("swarm@a1b2c3") is None
