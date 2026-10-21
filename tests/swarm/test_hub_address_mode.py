import io

import pytest

from scripts.scheduler import core, service
from scripts.swarm import cli, controller, stack_clients, stack_env, store
from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_ledger import ledger_link, ledger_route
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime

HUB = "https://hub.example:8765"
HIVE_REDIS = "rediss://hive-a@h:1/0"


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    monkeypatch.delenv("SWARM_URL", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    monkeypatch.setenv("LEDGER_URL", HUB)


@pytest.mark.parametrize(
    ("environ", "local"),
    [
        ({}, True),
        ({"LEDGER_URL": HUB}, False),
        ({"LEDGER_URL": HUB, "AGENTIHOOKS_DEPLOYMENT": "local"}, True),
        ({"AGENTIHOOKS_DEPLOYMENT": "compose"}, False),
        ({"AGENTIHOOKS_DEPLOYMENT": ""}, False),
    ],
)
def test_the_mode_rule_reads_a_hub_address_with_no_mode_as_remote(environ, local):
    assert ledger_link.local_mode(environ) is local


@pytest.fixture
def swarm_store():
    import fakeredis

    swarms = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    swarms.create(SwarmConfig("sw", ".", 1, 0, state="running"))
    return swarms


def test_the_tick_run_guard_leaves_a_hub_address_with_no_mode_to_the_controller(hub, swarm_store, monkeypatch):
    monkeypatch.setattr(cli, "tick", lambda *args: pytest.fail("a remote host must not run an ordinary tick"))
    ledger, runtime = FakeLedger([{"id": "t1", "lane": "eng", "state": "open"}]), FakeRuntime()

    assert cli.run_tick(swarm_store, "sw", ledger, runtime, FakeHerdr({})) == [cli.CONTROLLER_TICKS]


@pytest.mark.parametrize(
    ("machine", "hive_placement", "deployment"),
    [
        ({"LEDGER_URL": HUB}, False, "compose"),
        ({"LEDGER_URL": HUB}, True, "compose"),
        ({"LEDGER_URL": HUB, "AGENTIHOOKS_DEPLOYMENT": "local"}, True, "hybrid"),
        ({"LEDGER_URL": HUB, "AGENTIHOOKS_DEPLOYMENT": "distributed"}, False, "distributed"),
        ({}, False, "local"),
    ],
)
def test_the_controller_counts_a_hub_address_with_no_mode_as_remote(swarm_store, machine, hive_placement, deployment):
    config = swarm_store.update("sw", hive_placement=hive_placement)
    assert controller.deployment_for(config, machine) == deployment


def test_client_preparation_refuses_a_hub_address_with_no_mode(tmp_path):
    home = tmp_path / "agentihooks"
    home.mkdir()
    name = "SWARM_REDIS_PASSWORD"
    stack_env.write_private(home / ".env", f"{name}=bundle\n")
    out = io.StringIO()

    assert stack_clients.prepare(home, {"LEDGER_URL": HUB}, out) == 1
    assert out.getvalue() == "Client preparation requires the existing local deployment.\n"


@pytest.mark.parametrize(
    ("environ", "url"),
    [
        ({"LEDGER_URL": HUB, "AGENTIHOOKS_HIVE_REDIS_URL": HIVE_REDIS}, HIVE_REDIS),
        ({"LEDGER_URL": HUB, "AGENTIHOOKS_DEPLOYMENT": "local", "AGENTIHOOKS_HIVE_REDIS_URL": HIVE_REDIS}, None),
        ({"AGENTIHOOKS_HIVE_REDIS_URL": HIVE_REDIS}, None),
    ],
)
def test_the_swarm_redis_address_counts_a_hub_address_with_no_mode_as_remote(environ, url):
    assert store.redis_url(environ) == (url or store.DEFAULT_URL)


def test_a_hub_address_with_no_mode_adopts_the_saved_hive_member(monkeypatch):
    from scripts.hive import cli as hive_cli

    saved = {"AGENTIHOOKS_HIVE_ID": "new", "AGENTIHOOKS_HIVE_REDIS_URL": "rediss://hive-new@h:1/0"}
    monkeypatch.setattr(hive_cli, "saved_member", lambda environ: saved)
    environ = {"LEDGER_URL": HUB, "AGENTIHOOKS_HIVE_REDIS_URL": HIVE_REDIS}

    store._adopt_saved_member(environ)
    assert environ["AGENTIHOOKS_HIVE_REDIS_URL"] == "rediss://hive-new@h:1/0"


def test_the_scheduler_namespaces_a_hub_address_with_no_mode_as_remote(hub, monkeypatch):
    assert core.Scheduler(None, machine="m").key("entries") == f"{ROOT}:scheduler:m:entries"
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "member-9")
    assert core.Scheduler(None, machine="m").key("entries") == f"{ROOT}:hive:member-9:scheduler:m:entries"


def test_the_scheduler_runtime_folder_counts_a_hub_address_with_no_mode_as_remote():
    local = service.runtime_dir({"AGENTIHOOKS_HIVE_ID": "member-9"})
    hosted = service.runtime_dir({"LEDGER_URL": HUB, "AGENTIHOOKS_HIVE_ID": "member-9"})
    pinned = service.runtime_dir({"LEDGER_URL": HUB, "AGENTIHOOKS_DEPLOYMENT": "local", "AGENTIHOOKS_HIVE_ID": "m"})

    assert hosted.parent == local == pinned


def test_the_ledger_route_ignores_the_local_api_address_under_a_hub_address_with_no_mode(hub, swarm_store, monkeypatch):
    swarm_store.redis.hset(swarm_store.key("sw", "config"), "api_url", "http://127.0.0.1:8765")
    assert ledger_route.configured(swarm_store, "sw") == ""

    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    assert ledger_route.configured(swarm_store, "sw") == "http://127.0.0.1:8765"


def test_a_ledger_session_on_a_hub_address_with_no_mode_never_reads_the_local_store(hub, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setattr(ledger_route.ports, "swarm", lambda: pytest.fail("a remote session must not open the store"))

    with ledger_route.session("sw"):
        assert ledger_route.url("sw") == ""
