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
    assert naming.swarm_name(config.code) == f"swarm@{config.code}"
    assert store.names.swarm(config.code) == {
        "swarm": "sw",
        "name": naming.swarm_name(config.code),
        "ledger": "sw",
        "repo": "/home/x/dev/agentihooks",
    }


def test_the_slug_or_the_name_resolves_the_same_swarm(store):
    name = naming.swarm_name(store.config("sw").code)
    assert store.names.swarm_slug("sw") == "sw"
    assert store.names.swarm_slug(name) == "sw"
    assert store.names.swarm_slug("swarm@ffffff") == "swarm@ffffff"
    with pytest.raises(SwarmError, match="no swarm swarm@ffffff"):
        store.config(store.names.swarm_slug("swarm@ffffff"))


def test_agent_alias_timeout_preserves_the_name_and_logs_the_failure(store, monkeypatch, caplog):
    from redis.exceptions import TimeoutError

    def timeout(*args):
        raise TimeoutError("Timeout reading from socket")

    monkeypatch.setattr(store.redis, "get", timeout)
    assert store.names.resolve("engineer") == "engineer"
    assert caplog.record_tuples == [
        ("scripts.swarm.naming", 30, "alias lookup failed for engineer: Timeout reading from socket")
    ]


@pytest.mark.parametrize("lookup", ["swarm", "code_of"])
def test_swarm_slug_preserves_the_reference_on_a_store_timeout(store, monkeypatch, caplog, lookup):
    from redis.exceptions import TimeoutError

    name = naming.swarm_name(store.config("sw").code)

    def timeout(*args):
        raise TimeoutError("Timeout reading from socket")

    monkeypatch.setattr(store.names, lookup, timeout)
    assert store.names.swarm_slug(name) == name
    assert caplog.record_tuples == [
        ("scripts.swarm.naming", 30, f"swarm alias lookup failed for {name}: Timeout reading from socket")
    ]


def test_settings_by_slug_survive_a_swarm_alias_timeout(env, monkeypatch, caplog):  # noqa: F811
    from redis.exceptions import TimeoutError

    store, _, _ = env
    run("sw", "create", "--repo", "/repo")

    def timeout(*args):
        raise TimeoutError("Timeout reading from socket")

    monkeypatch.setattr(store.names, "swarm", timeout)
    assert run("sw", "set", "max-eng-agents=4") == 0
    assert store.config("sw").max_eng == 4
    assert "swarm alias lookup failed for sw" in caplog.text


def test_a_removed_swarm_name_no_longer_resolves(store):
    name = naming.swarm_name(store.config("sw").code)
    store.remove("sw")
    assert store.names.swarm_slug(name) == name


def test_the_name_is_derived_from_the_code_and_never_changes(store):
    code = store.config("sw").code
    assert store.update("sw", state="paused").code == code
    assert store.ensure_code("sw").code == code
    assert naming.swarm_name(code) == f"swarm@{code}"
    assert naming.swarm_name("") == ""


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
    store.redis.hset(store.key("old", "config"), "code", "")
    store.names.release("old")
    assert naming.swarm_name(store.config("old").code) == ""
    assert run("rename") == 0
    config = store.config("old")
    assert naming.swarm_name(config.code) == f"swarm@{config.code}"
    assert [a.name for a in store.agents("old")] == [agent]
    assert store.claimant("old", "t1") == agent
    assert store.seats.occupant(seat).occupant == agent
    assert store.names.swarm_slug(naming.swarm_name(config.code)) == "old"
    assert [i.id for i in InboxStore(store.redis).pending_mail(agent)] == [item.id]


def test_status_list_and_names_show_the_swarm_name(env, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    name = naming.swarm_name(store.config("sw").code)
    capsys.readouterr()
    run("sw", "status")
    assert capsys.readouterr().out.splitlines()[0].startswith(f"{name}  sw  paused")
    run("sw", "status", "--json")
    assert json.loads(capsys.readouterr().out)["config"]["name"] == name
    store.create(SwarmConfig("old", "/repo", 1, 0))
    store.redis.hset(store.key("old", "config"), "code", "")
    run("list")
    assert capsys.readouterr().out.splitlines() == [
        "-\told\trunning\teng 1\tci 0\tplan 1\tscaling auto\tagents 0\t/repo",
        f"{name}\tsw\tpaused\teng 2\tci 1\tplan 1\tscaling auto\tagents 0\t/repo",
    ]
    run("sw", "names")
    assert capsys.readouterr().out.startswith(f"name {name}\tcode ")


def test_a_command_given_the_name_acts_on_the_swarm(env, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    name = naming.swarm_name(store.config("sw").code)
    assert run(name, "pause") == 0
    assert store.config("sw").state == "paused"
    capsys.readouterr()
    assert run(name, "status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["config"]["slug"] == "sw"


def test_a_name_cannot_create_a_swarm(env, capsys):  # noqa: F811
    assert run("swarm@a1b2c3", "create", "--repo", "/repo") == 1
    assert cli.SLUG_RE.match("swarm@a1b2c3") is None


def test_a_swarm_registry_emptied_by_a_restore_is_adopted_with_its_name(store):
    config = store.config("sw")
    store.redis.delete(naming.NameRegistry.key("codes"), naming.NameRegistry.key("code-of"))
    assert store.names.swarm_slug(naming.swarm_name(config.code)) == naming.swarm_name(config.code)
    store.ensure_code("sw")
    assert store.names.swarm(config.code) == {
        "swarm": "sw",
        "name": naming.swarm_name(config.code),
        "ledger": "sw",
        "repo": "/home/x/dev/agentihooks",
    }
    assert store.names.swarm_slug(naming.swarm_name(config.code)) == "sw"


def test_status_of_a_swarm_not_yet_named_marks_the_name_missing(env, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    store.redis.hset(store.key("sw", "config"), "code", "")
    capsys.readouterr()
    run("sw", "status")
    assert capsys.readouterr().out.splitlines()[0].startswith("-  sw  paused  eng 2")


def test_names_json_carries_the_swarm_name(env, capsys):  # noqa: F811
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    capsys.readouterr()
    run("sw", "names", "--json")
    config = store.config("sw")
    assert json.loads(capsys.readouterr().out) == {
        "name": naming.swarm_name(config.code),
        "code": config.code,
        "space": f"repo-{config.code}",
        "names": [],
    }
