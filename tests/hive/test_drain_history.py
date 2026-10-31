import pytest

from scripts.doctor import hive_pools
from scripts.hive import registry

pytestmark = pytest.mark.unit

NOW = 1_793_166_000_000
HIVE = "06db8fa06259f56a"


@pytest.fixture
def client(monkeypatch):
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    monkeypatch.setattr(registry, "now_ms", lambda: NOW)
    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def _live(client, hive_id=HIVE):
    registry.beat(client, hive_id, {"heartbeat_at": NOW - 1_000})


def test_a_drained_then_undrained_hive_still_names_who_drained_it_why_and_when(client):
    _live(client)
    registry.update(client, HIVE, ["draining=yes"], drain=registry.Drain("worker-0", "release roll", NOW + 600_000))
    registry.update(client, HIVE, ["draining=no"], drain=registry.Drain("worker-1", "hive start"))

    record = registry.show(client, HIVE)

    assert record["draining"] == "no"
    assert record["drains"] == [
        {
            "at": NOW,
            "draining": "yes",
            "owner": "worker-0",
            "reason": "release roll",
            "deadline_at": NOW + 600_000,
            "ready_before": True,
            "ready_after": False,
        },
        {
            "at": NOW,
            "draining": "no",
            "owner": "worker-1",
            "reason": "hive start",
            "deadline_at": 0,
            "ready_before": False,
            "ready_after": True,
        },
    ]


def test_a_setting_that_leaves_the_drain_flag_alone_records_nothing(client):
    _live(client)
    registry.update(client, HIVE, ["draining=no", "max-agents=4"])

    assert "drains" not in registry.show(client, HIVE)


def test_a_drain_without_a_named_owner_is_owned_by_the_agent_and_the_hive_set_command(client, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@1")
    _live(client)
    registry.update(client, HIVE, ["draining=yes"])

    [entry] = registry.show(client, HIVE)["drains"]
    assert (entry["owner"], entry["reason"], entry["deadline_at"]) == ("engineer@1", "hive set", 0)


def test_the_drain_history_keeps_only_the_newest_entries(client):
    _live(client)
    for turn in range(registry.DRAINS_KEPT + 3):
        flag = "yes" if turn % 2 == 0 else "no"
        registry.update(client, HIVE, [f"draining={flag}"], drain=registry.Drain(f"owner-{turn}", "roll"))

    drains = registry.show(client, HIVE)["drains"]
    assert len(drains) == registry.DRAINS_KEPT
    assert drains[-1]["owner"] == f"owner-{registry.DRAINS_KEPT + 2}"
    assert drains[0]["owner"] == "owner-3"


def test_a_down_pool_finding_names_who_drained_each_hive_why_and_by_when():
    reading = {"cpus": 16, "total_mb": 64000, "pod_total_mb": 19600}
    drain = {
        "at": NOW,
        "draining": "yes",
        "owner": "worker-0",
        "reason": "release roll",
        "deadline_at": NOW + 600_000,
        "ready_before": True,
        "ready_after": False,
    }
    hive = {"id": HIVE, "ui": "no", "draining": "yes", "heartbeat_at": NOW - 1_000, "drains": [drain]}

    state = hive_pools.observe({}, [hive], {HIVE: reading}, [], NOW)
    [found] = hive_pools.findings(state, NOW + 10 * 60_000)

    assert state["down"]["16cpu-19600mb"]["drains"] == {HIVE: drain}
    assert found.evidence[-1] == (
        f"hive {HIVE} draining yes by worker-0 at {NOW} for release roll, deadline {NOW + 600_000}"
    )
