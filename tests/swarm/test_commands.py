import json

import pytest

from scripts.swarm import commands
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store():
    import fakeredis

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


def test_command_identity_digest_and_receipt_clock_survive_delivery(store, monkeypatch):
    import re
    from types import SimpleNamespace

    stamps = iter([1_234_000_000, 2_345_000_000, 3_456_789_012])
    monkeypatch.setattr(commands, "time", SimpleNamespace(time_ns=lambda: next(stamps)))
    sent = commands.submit(store, "sw", "swarm", ["pause"])
    assert re.fullmatch("[0-9a-f]{32}", sent["id"])
    assert sent["digest"] == "ca2f036c50ae145f0ec02ce2f10db171751bbb45d00bdba84a8b3a4c19552c69"
    assert sent["created_at"] == 1234
    assert isinstance(sent["created_at"], int)

    def execute(row):
        held = commands.rows(store, "sw")[0]
        assert held["state"] == "accepted"
        assert held["owner"] == "home"
        assert held["accepted_at"] == 2345
        assert isinstance(held["accepted_at"], int)
        assert held["digest"] == sent["digest"]
        return ""

    assert commands.consume(store, "sw", "home", execute) == ["control swarm acknowledged"]
    result = commands.rows(store, "sw")[0]
    assert result["id"] == sent["id"]
    assert result["acknowledged_at"] == 3456
    assert isinstance(result["acknowledged_at"], int)


def test_a_tick_drains_more_than_two_pending_controls(store):
    sent = [commands.submit(store, "sw", "swarm", ["pause"]) for _ in range(3)]
    seen = []
    commands.consume(store, "sw", "home", lambda row: seen.append(row["id"]) or "")
    assert seen == [row["id"] for row in sent]


def test_accepted_work_does_not_prevent_delivery_of_later_controls(store):
    commands.submit(store, "sw", "swarm", ["pause"])

    def crash(row):
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError):
        commands.consume(store, "sw", "home", crash)
    sent = [commands.submit(store, "sw", "swarm", ["start"]) for _ in range(2)]
    seen = []
    commands.consume(store, "sw", "home", lambda row: seen.append(row["id"]) or "")
    assert seen == [row["id"] for row in sent]


def test_an_old_owner_cannot_hide_later_controls_or_its_pending_record(store):
    commands.bind(store, "sw", "former")
    old = commands.submit(store, "sw", "swarm", ["pause"])
    store.redis.delete(store.key("sw", "control-owner"))
    commands.bind(store, "sw", "home")
    for _ in range(24):
        commands.submit(store, "sw", "swarm", ["start"])
        commands.consume(store, "sw", "home", lambda row: "")
    found = commands.rows(store, "sw")
    assert len(found) == 21
    assert found[0]["id"] == old["id"]
    assert found[0]["state"] == "pending"


def test_old_acceptance_survives_newer_acknowledgements(store):
    held = commands.submit(store, "sw", "swarm", ["pause"])

    def crash(row):
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError):
        commands.consume(store, "sw", "home", crash)
    for _ in range(24):
        commands.submit(store, "sw", "swarm", ["start"])
        commands.consume(store, "sw", "home", lambda row: "")
    assert commands.rows(store, "sw")[0]["id"] == held["id"]
    assert commands.rows(store, "sw")[0]["state"] == "accepted"


@pytest.mark.parametrize("accepted", [False, True])
def test_stop_snapshot_preserves_restored_duplicate_deliveries(store, accepted):
    commands.bind(store, "sw", "home")
    first = commands.submit(store, "sw", "swarm", ["pause"])

    def crash(row):
        raise RuntimeError("interrupted")

    if accepted:
        with pytest.raises(RuntimeError):
            commands.consume(store, "sw", "home", crash)
    stop = commands.submit(store, "sw", "swarm", ["stop"])
    saved = store.export("sw")
    key = store.key("sw", "command-pending")
    saved["keys"][key]["value"].append(first["id"])
    store.restore("sw", saved)
    checkpoints = []

    def execute(row):
        if row["id"] == stop["id"]:
            checkpoints.append(store.export("sw")["keys"][key]["value"])
        return ""

    commands.consume(store, "sw", "home", execute)
    assert checkpoints == [[stop["id"], first["id"]]]


def test_status_and_workspaces_before_the_first_hive_publication(store):
    view = commands.view(store, "sw")
    assert view["agents"] == []
    assert view["tasks"] == {}
    assert view["config"]["slug"] == "sw"
    assert view["config"]["state"] == "paused"
    assert commands.workspaces(store, "sw") == {}
    store.redis.set(store.key("sw", "published"), '{"status": {}}')
    assert commands.workspaces(store, "sw") == {}


def test_status_keeps_published_config_labels_with_current_control_state(store):
    commands.publish(store, "sw", {"config": {"name": "swarm name", "state": "running", "snapshot_minutes": 25}}, {})
    view = commands.view(store, "sw")
    assert view["config"] == {"name": "swarm name", "state": "paused", "snapshot_minutes": 25}
