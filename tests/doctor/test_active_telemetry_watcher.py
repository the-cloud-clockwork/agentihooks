import copy
import json

import pytest

from hooks.observability import agent_trace, trace_flush
from scripts.doctor import loop, registry, traces
from scripts.inbox.store import InboxStore
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


@pytest.mark.parametrize("root_only", [False, True])
def test_a_claude_backlog_without_a_request_alerts_the_master_again_after_recovery(
    store, tmp_path, monkeypatch, root_only
):
    monkeypatch.setattr(agent_trace, "CURSOR_DIR", tmp_path)
    store.set_peer("watch-doctor", "watched")
    path = agent_trace._cursor_path("sid-1")
    assert not trace_flush.request_path("sid-1").exists()

    def cursor(since, accepted_bytes=0):
        pending = [{"parent_id": None, "start_ns": (NOW - 600_000) * 1_000_000, "end_ns": (since + 10_000) * 1_000_000}]
        if not root_only:
            pending += [
                {"parent_id": 1, "start_ns": (since + 5_000) * 1_000_000, "end_ns": (since + 9_000) * 1_000_000},
                {"parent_id": 1, "start_ns": since * 1_000_000, "end_ns": (since + 7_000) * 1_000_000},
            ]
        path.write_text(
            json.dumps(
                {
                    "source": {"buffered_bytes": 5000, "accepted_bytes": accepted_bytes},
                    "pending": pending if accepted_bytes == 0 else [],
                    "accepted": {str(n): "revision" for n in range(40)},
                }
            )
        )

    def scan(at):
        record = _record(_binding(local=registry.progress("sid-1", "claude")))
        record["now_ms"] = at
        found = traces.findings(record, LIMITS)
        actions = loop.run(store, "watch-doctor", at, lambda watched: (found, []), lambda: None, {})
        return found, actions

    cursor(NOW - 90_000)
    found, actions = scan(NOW)
    assert [f.kind for f in found] == ["exporter backlog"]
    first_id = found[0].id
    assert actions == ["doctor pass: 1 new finding sent to the Doctor master"]
    assert scan(NOW + 600_000)[1] == []
    cursor(NOW - 90_000, accepted_bytes=5000)
    assert scan(NOW + 1_200_000) == ([], [])

    cursor(NOW + 1_500_000)
    found, actions = scan(NOW + 1_800_000)
    assert [f.kind for f in found] == ["exporter backlog"]
    assert actions == ["doctor pass: 1 new finding sent to the Doctor master"]
    second_id = found[0].id
    assert second_id != first_id
    assert scan(NOW + 2_400_000)[1] == []
    items = InboxStore(store.redis).inbox("master@watch-doctor")
    assert len(items) == 2
    assert all(item.sender == loop.SENDER for item in items)
    assert sum(first_id in item.text for item in items) == 1
    assert sum(second_id in item.text for item in items) == 1
