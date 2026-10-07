import copy

import pytest

from scripts.doctor import loop, traces
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.doctor.test_active_telemetry import AGENT, LIMITS, NOW, _binding, _local, _record

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig("watch-doctor", "/repo", 1, 0, state="drained", template="doctor"))
    return found


def test_the_watcher_sends_one_finding_per_fault_clears_on_recovery_and_rearms_on_a_new_fault(store):
    def scan(binding, at):
        record = copy.deepcopy(_record(binding))
        record["now_ms"] = at
        found = traces.findings(record, LIMITS)
        return found, loop.record(store, "watch-doctor", found, at, 60 * 60_000)

    fault = _binding(local=_local(generated_bytes=5000, oldest_unaccepted=NOW - 90_000, pending=4))
    found, new = scan(fault, NOW)
    assert [n["kind"] for n in new] == ["exporter backlog"]
    later = copy.deepcopy(fault)
    later["local"]["generated_bytes"] = 9000
    found, new = scan(later, NOW + 600_000)
    assert len(found) == 1 and new == []
    found, new = scan(_binding(), NOW + 1_200_000)
    assert found == [] and new == []
    again = _binding(local=_local(generated_bytes=9000, oldest_unaccepted=NOW + 1_500_000, pending=2))
    found, new = scan(again, NOW + 1_800_000)
    assert [n["kind"] for n in new] == ["exporter backlog"]
    assert new[0]["id"] != f"exporter-backlog/{AGENT}.{NOW - 90_000}"
