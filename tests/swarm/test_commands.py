import json

import fakeredis
import pytest

from scripts.swarm import commands
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store():
    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("sw", ".", 0, 0, state="paused"))
    return result


def test_commands_stay_pending_until_the_owning_tick_acknowledges(store):
    commands.bind(store, "sw", "home")
    row = commands.submit(store, "sw", "swarm", ["pause"])
    assert row["state"] == "pending"
    assert row["owner"] == "home"
    assert commands.consume(store, "sw", "other", lambda row: "") == []
    calls = []
    result = commands.consume(store, "sw", "home", lambda row: calls.append(row) or "")
    assert len(result) == len(calls) == 1
    assert commands.rows(store, "sw")[0]["state"] == "acknowledged"
    assert commands.consume(store, "sw", "home", lambda row: calls.append(row)) == []
    assert len(calls) == 1


def test_failure_is_retained_without_replaying_an_effect(store):
    commands.bind(store, "sw", "home")
    commands.submit(store, "sw", "quota", [])
    commands.consume(store, "sw", "home", lambda row: "probe timed out")
    row = commands.rows(store, "sw")[0]
    assert row["state"] == "failed"
    assert row["error"] == "probe timed out"
    assert row["accepted_at"] <= row["acknowledged_at"]
    assert commands.consume(store, "sw", "home", lambda row: pytest.fail("replayed")) == []


def test_published_view_carries_telemetry_without_local_readers(store):
    commands.publish(store, "sw", {"quota": {"rows": ["account"]}}, {"t": {"latest_proof": "green"}})
    assert commands.view(store, "sw")["quota"] == {"rows": ["account"]}
    assert commands.workspaces(store, "sw") == {"t": {"latest_proof": "green"}}
    commands.submit(store, "sw", "swarm", ["pause"])
    assert commands.view(store, "sw")["commands"][0]["state"] == "pending"
    assert json.loads(store.redis.get(store.key("sw", "published")))["workspaces"]["t"]["latest_proof"] == "green"


def test_failed_controls_remain_visible_after_twenty_new_acknowledgements(store):
    commands.bind(store, "sw", "home")
    failed = commands.submit(store, "sw", "swarm", ["pause"])
    commands.consume(store, "sw", "home", lambda row: "pause refused")
    for _ in range(24):
        commands.submit(store, "sw", "swarm", ["start"])
        commands.consume(store, "sw", "home", lambda row: "")
    found = commands.rows(store, "sw")
    assert len(found) == 21
    assert found[0]["id"] == failed["id"]
    assert found[0]["state"] == "failed"
    assert all(row["state"] == "acknowledged" for row in found[1:])


def test_an_execution_exception_retains_acceptance_without_replaying(store):
    commands.bind(store, "sw", "home")
    sent = commands.submit(store, "sw", "swarm", ["pause"])

    def crash(row):
        raise RuntimeError("hive interrupted")

    with pytest.raises(RuntimeError, match="hive interrupted"):
        commands.consume(store, "sw", "home", crash)
    assert commands.rows(store, "sw")[0]["id"] == sent["id"]
    assert commands.rows(store, "sw")[0]["state"] == "accepted"
    assert commands.consume(store, "sw", "home", lambda row: pytest.fail("effect replayed")) == []
    assert commands.rows(store, "sw")[0]["state"] == "accepted"
