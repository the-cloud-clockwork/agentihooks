import pytest

from scripts.swarm import cli, commands, controller, lease
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime


@pytest.mark.parametrize("mode", ["compose", "distributed"])
def test_an_agent_pod_tick_takes_no_controller_lease_and_never_spawns_or_reaps(monkeypatch, mode):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", ".", 1, 0, state="running"))
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", mode)
    passes = []
    monkeypatch.setattr(cli, "tick", lambda slug, *args: passes.append(slug) or [])
    ledger, rt = FakeLedger([{"id": "t1", "lane": "eng", "state": "open"}]), FakeRuntime()

    actions = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert lease.current(store, "sw") is None
    assert (passes, rt.spawned, rt.killed) == ([], [], [])
    assert actions == [cli.CONTROLLER_TICKS] == ["the controller tick spawns and reaps agents"]

    assert list(controller.run_once(store, ledger=ledger, runtime=rt, messenger=FakeHerdr({}))) == ["sw"]
    assert lease.current(store, "sw").owner == commands.hive_id()
    assert passes == ["sw"]
