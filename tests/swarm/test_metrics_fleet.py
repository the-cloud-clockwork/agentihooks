import json

import pytest

from scripts.swarm import metrics_fleet
from scripts.swarm.metrics_outbox import Outbox, Settings
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2 import quota

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
SLUG = "scratch"
NOW = 1_800_000_000_000


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(SLUG, ".", 0, 0))
    return found


def seeded(store):
    attached = AgentRecord(store.next_name(SLUG, "eng"), "eng", "t1", pane_id="w1:p1", seat=f"eng-1@{SLUG}")
    detached = AgentRecord(store.next_name(SLUG, "eng"), "eng", "t2", seat=f"eng-2@{SLUG}")
    ex1 = store.execution_registry.start(SLUG, attached, "").execution_id
    ex2 = store.execution_registry.start(SLUG, detached, "").execution_id
    beat = {"state": "working", "accepted_at_ms": NOW - 1_000, "archive_watermark": 3}
    stale = {**beat, "archive_advanced_at_ms": NOW - 600_000}
    store.redis.hset(store.key(SLUG, "heartbeats"), mapping={ex1: json.dumps(beat), ex2: json.dumps(stale)})
    pending = {"reservation": "r1", "deadline_ms": NOW + 60_000, "resources": {"cpu_millis": 500, "memory_mib": 512}}
    store.redis.hset(store.key(SLUG, "pending-admission"), "t3", json.dumps(pending))
    seen = quota.Observation("acct", "claude", "ex1", 1, NOW - 5_000, "rejected", 100.0, 40.0, 1.0, 2.0)
    store.redis.hset(quota.latest_key("claude"), "acct", quota.encode(seen))
    store.redis.set(quota.wait_key("acct", "claude"), NOW + 120_000)
    free = quota.Observation("spare", "codex", "ex2", 1, NOW - 5_000, "allowed", 10.0, 10.0, 1.0, 2.0)
    store.redis.hset(quota.latest_key("codex"), "spare", quota.encode(free))


def test_the_sample_reads_the_registry_admissions_quota_archive_and_feed(store):
    seeded(store)
    found = metrics_fleet.sample(store, SLUG, NOW, NOW - 30_000)
    attempts = {row["task"]: row for row in found["attempts"]}
    assert {key: row["phase"] for key, row in attempts.items()} == {"t1": "active", "t2": "active", "t3": "pending"}
    assert attempts["t1"]["execution_id"]
    assert attempts["t1"] == {
        "execution_id": attempts["t1"]["execution_id"],
        "seat": f"eng-1@{SLUG}",
        "task": "t1",
        "phase": "active",
        "terminal": "attached",
        "authority": "working",
    }
    assert attempts["t2"]["terminal"] == "detached"
    assert attempts["t3"] == {
        "execution_id": "",
        "seat": "",
        "task": "t3",
        "phase": "pending",
        "requested": {"cpu_millis": 500, "memory_mib": 512},
        "terminal": "detached",
        "authority": "pending",
    }
    assert {row["observed_at_ms"] for row in found["archive"]} == {NOW}
    waiting = {row["account"]: row for row in found["quota"]}
    assert waiting["acct"] == {
        "account": "acct",
        "state": "waiting",
        "since_ms": NOW + 120_000 - quota.WAIT_MS,
        "resets_at_ms": NOW + 120_000,
    }
    assert waiting["spare"]["state"] == "allowed"
    assert {row["execution_id"] for row in found["archive"]} == {
        attempts["t1"]["execution_id"],
        attempts["t2"]["execution_id"],
    }
    assert found["brain"] == {"feed_at_ms": NOW - 30_000, "denials": []}
    assert found["pools"] == [] and found["checkpoints"] == []
    assert (found["seq"], found["at_ms"]) == (NOW, NOW)


def test_record_exports_the_dashboard_panels_and_one_coverage_row_per_source(tmp_path, store):
    seeded(store)
    box = Outbox(tmp_path / "outbox.db", Settings("http://sink", "u", ""))
    try:
        metrics_fleet.record(box, store, SLUG, NOW, lambda: NOW - 30_000)
        panels = {
            (row["panel"], row["series"], row["subject"]): row["value"] for row in box.recent("fleet_panels", NOW)
        }
        assert panels[("attempts", "active", "")] == 2.0
        assert panels[("attempts", "pending", "")] == 1.0
        assert panels[("quota_wait", "waiting_accounts", "")] == 1.0
        assert panels[("quota_wait", "longest_wait_ms", "")] == float(quota.WAIT_MS - 120_000)
        assert panels[("archive_lag", "lagging", "")] == 1.0
        assert panels[("archive_lag", "worst_lag_ms", "")] == 600_000.0
        assert panels[("connectivity", "terminal", "attached")] == 1.0
        assert panels[("connectivity", "terminal", "detached")] == 2.0
        assert panels[("connectivity", "authority", "working")] == 2.0
        assert panels[("knowledge", "feed_age_ms", "")] == 30_000.0
        assert panels[("knowledge", "stale", "")] == 0.0
        assert panels[("checkpoint_age", "stale", "")] == 0.0
        sources = {row["source"]: (row["available"], row["readings"]) for row in box.recent("fleet_sources", NOW)}
        assert sources == {
            "executions": (1, 2),
            "pending_admissions": (1, 1),
            "quota": (1, 2),
            "archive": (1, 2),
            "brain_feed": (1, 1),
            "pools": (0, 0),
            "checkpoints": (0, 0),
            "brain_denials": (0, 0),
        }
        rows = box.recent("fleet_panels", NOW) + box.recent("fleet_sources", NOW)
        assert all(row["ledger"] == SLUG and row["ts_ms"] == NOW for row in rows)
        assert len({row["event_id"] for row in rows}) == len(rows)
    finally:
        box.close()


def test_a_missing_brain_feed_is_an_unavailable_source_not_a_fresh_one(tmp_path, store):
    box = Outbox(tmp_path / "outbox.db", Settings("http://sink", "u", ""))
    try:
        metrics_fleet.record(box, store, SLUG, NOW, lambda: 0)
        panels = {(row["panel"], row["series"]): row["value"] for row in box.recent("fleet_panels", NOW)}
        assert ("knowledge", "feed_age_ms") not in panels
        sources = {row["source"]: (row["available"], row["readings"]) for row in box.recent("fleet_sources", NOW)}
        assert sources["brain_feed"] == (0, 0)
        assert sources["executions"] == (1, 0)
        assert panels[("attempts", "active")] == 0.0
        assert panels[("knowledge", "cross_scope_denials")] == 0.0
    finally:
        box.close()


def test_an_occupant_without_a_heartbeat_is_local_and_an_ended_wait_is_allowed(store):
    record = AgentRecord(store.next_name(SLUG, "eng"), "eng", "t1", seat=f"eng-1@{SLUG}")
    store.execution_registry.start(SLUG, record, "")
    seen = quota.Observation("acct", "claude", "ex1", 1, NOW - 5_000, "allowed", 10.0, 10.0, 1.0, 2.0)
    store.redis.hset(quota.latest_key("claude"), "acct", quota.encode(seen))
    store.redis.set(quota.wait_key("acct", "claude"), NOW)
    found = metrics_fleet.sample(store, SLUG, NOW, NOW)
    assert [row["authority"] for row in found["attempts"]] == ["local"]
    assert found["quota"] == [{"account": "acct", "state": "allowed", "since_ms": NOW, "resets_at_ms": NOW}]


def test_panel_rows_carry_no_plan_path():
    rows = metrics_fleet.panel_rows(SLUG, NOW, {"attempts": {"active": {"label": "a", "value": 1}}}, True)
    assert [(row["plan"], row["phase"], row["slice"], row["task"], row["value"]) for row in rows] == [
        ("", "", "", "", 1.0)
    ]
    found = {"attempts": [], "quota": [], "archive": [], "brain": {"feed_at_ms": 0}}
    assert {
        (row["plan"], row["phase"], row["slice"], row["task"]) for row in metrics_fleet.source_rows(SLUG, NOW, found)
    } == {("", "", "", "")}


def test_the_feed_time_is_the_cached_feed_file_time(tmp_path, monkeypatch):
    feed = tmp_path / "feed.json"
    monkeypatch.setattr(metrics_fleet, "feed_path", lambda: feed)
    assert metrics_fleet.feed_at_ms() == 0
    feed.write_text("{}")
    assert metrics_fleet.feed_at_ms() == int(feed.stat().st_mtime * 1000)
