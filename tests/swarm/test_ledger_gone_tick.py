import pytest

from scripts.swarm import cli
from scripts.swarm.ledger_client import LedgerGone
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")


class GoneLedger(FakeLedger):
    def state(self, slug):
        raise LedgerGone(f"ledger {slug} does not exist")


def test_a_swarm_whose_ledger_is_gone_is_reported_once_then_left_quiet():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    ledger, rt = GoneLedger([]), FakeRuntime()
    first = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert first == ["ledger sw does not exist; agentihooks swarm remove sw clears this swarm once it has no agents"]
    assert cli.run_tick(store, "sw", ledger, rt, FakeHerdr({})) == []
    assert rt.spawned == [] and store.redis.get(store.key("sw", "ledger-gone")) == "1"
