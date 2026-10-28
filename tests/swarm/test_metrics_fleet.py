import json

import pytest

from scripts.swarm import metrics_fleet
from scripts.swarm.metrics_outbox import Outbox, Settings
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2 import quota

pytestmark = pytest.mark.unit
SLUG = "scratch"
NOW = 1_800_000_000_000


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(SLUG, ".", 0, 0))
    return found


def seeded(store):
    started = NOW - 1_000
    attached = AgentRecord(
        store.next_name(SLUG, "eng"), "eng", "t1", pane_id="w1:p1", seat=f"eng-1@{SLUG}", started_at=started
    )
    detached = AgentRecord(store.next_name(SLUG, "eng"), "eng", "t2", seat=f"eng-2@{SLUG}", started_at=started)
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
    assert found["pools"] == []
    assert found["checkpoints"] == [
        {"execution_id": attempts[task]["execution_id"], "seat": attempts[task]["seat"], "taken_at_ms": NOW - 1_000}
        for task in ("t1", "t2")
    ]
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
            "checkpoints": (1, 2),
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
        assert ("knowledge", "cross_scope_denials") not in panels
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
    found = {"attempts": [], "quota": [], "archive": [], "checkpoints": [], "brain": {"feed_at_ms": 0}}
    assert {
        (row["plan"], row["phase"], row["slice"], row["task"]) for row in metrics_fleet.source_rows(SLUG, NOW, found)
    } == {("", "", "", "")}


def test_the_feed_time_is_the_cached_feed_file_time(tmp_path, monkeypatch):
    feed = tmp_path / "feed.json"
    monkeypatch.setattr(metrics_fleet, "feed_path", lambda: feed)
    assert metrics_fleet.feed_at_ms() == 0
    feed.write_text("{}")
    assert metrics_fleet.feed_at_ms() == int(feed.stat().st_mtime * 1000)


def _recorded(tmp_path, store, pools=None):
    box = Outbox(tmp_path / "outbox.db", Settings("http://sink", "u", ""))
    try:
        metrics_fleet.record(box, store, SLUG, NOW, lambda: NOW, pools)
        return box.recent("fleet_panels", NOW), box.recent("fleet_sources", NOW)
    finally:
        box.close()


def test_the_newest_checkpoint_of_each_active_execution_reaches_the_sink(tmp_path, store, monkeypatch):
    from scripts.events import state

    clock = [NOW / 1000]
    monkeypatch.setattr(metrics_fleet.time, "time", lambda: clock[0])
    saved = state.for_store(store, metrics_fleet.CHECKPOINTS)
    seeded(store)
    executions = sorted(store.execution_occupants(SLUG).values(), key=lambda agent: agent.task)
    first, second = (agent.execution_id for agent in executions)
    metrics_fleet.record_checkpoint(store, SLUG, first, f"eng-1@{SLUG}", NOW - 3_000_000)
    metrics_fleet.record_checkpoint(store, SLUG, first, f"eng-1@{SLUG}", NOW - 2_000_000)
    metrics_fleet.record_checkpoint(store, SLUG, second, f"eng-2@{SLUG}", NOW - 1_800_000)
    metrics_fleet.record_checkpoint(store, SLUG, "ended", "eng-9@scratch", NOW - 9_000_000)
    before = json.loads(saved.get(SLUG).value)
    assert before[first]["expires_at_ms"] == NOW + metrics_fleet.CHECKPOINT_TTL_MS
    clock[0] += 1
    found = metrics_fleet.sample(store, SLUG, NOW, NOW)
    after = json.loads(saved.get(SLUG).value)
    assert after[first]["expires_at_ms"] == NOW + 1000 + metrics_fleet.CHECKPOINT_TTL_MS
    assert after["ended"]["expires_at_ms"] == before["ended"]["expires_at_ms"]
    assert found["checkpoints"] == [
        {"execution_id": first, "seat": f"eng-1@{SLUG}", "taken_at_ms": NOW - 2_000_000},
        {"execution_id": second, "seat": f"eng-2@{SLUG}", "taken_at_ms": NOW - 1_800_000},
    ]
    panels, sources = _recorded(tmp_path, store)
    values = {(row["panel"], row["series"]): row["value"] for row in panels}
    assert values[("checkpoint_age", "stale")] == 1.0
    assert values[("checkpoint_age", "oldest_age_ms")] == 2_000_000.0
    assert values[("knowledge", "feed_age_ms")] == 0.0
    assert ("knowledge", "cross_scope_denials") not in values
    assert {row["source"]: (row["available"], row["readings"]) for row in sources}["checkpoints"] == (1, 2)


def test_an_execution_without_a_checkpoint_ages_from_its_start(tmp_path, store):
    late = AgentRecord(store.next_name(SLUG, "eng"), "eng", "t1", seat=f"eng-1@{SLUG}", started_at=NOW - 1_900_000)
    fresh = AgentRecord(store.next_name(SLUG, "eng"), "eng", "t2", seat=f"eng-2@{SLUG}", started_at=NOW - 5_000)
    late_id = store.execution_registry.start(SLUG, late, "").execution_id
    fresh_id = store.execution_registry.start(SLUG, fresh, "").execution_id
    metrics_fleet.record_checkpoint(store, SLUG, fresh_id, f"eng-2@{SLUG}", NOW - 1_000)
    assert metrics_fleet.sample(store, SLUG, NOW, NOW)["checkpoints"] == [
        {"execution_id": late_id, "seat": f"eng-1@{SLUG}", "taken_at_ms": NOW - 1_900_000},
        {"execution_id": fresh_id, "seat": f"eng-2@{SLUG}", "taken_at_ms": NOW - 1_000},
    ]
    panels, _ = _recorded(tmp_path, store)
    values = {(row["panel"], row["series"]): row["value"] for row in panels}
    assert (values[("checkpoint_age", "stale")], values[("checkpoint_age", "oldest_age_ms")]) == (1.0, 1_900_000.0)


def test_a_worker_pool_reading_reaches_the_sink_per_pool(tmp_path, store):
    seeded(store)
    target = {"pod_namespace": "swarm-workers", "pod_name": "worker-t4"}
    pod = AgentRecord(
        store.next_name(SLUG, "eng"),
        "eng",
        "t4",
        seat=f"eng-3@{SLUG}",
        runtime_backend="kubernetes",
        runtime_target=target,
    )
    store.execution_registry.start(SLUG, pod, "")
    reading = [{"pool": "swarm-workers", "configured_maximum": 8, "nodes": 2}]
    found = metrics_fleet.sample(store, SLUG, NOW, NOW, reading)
    assert {row["task"]: row.get("pool") for row in found["attempts"]} == {
        "t1": None,
        "t2": None,
        "t3": "swarm-workers",
        "t4": "swarm-workers",
    }
    panels, sources = _recorded(tmp_path, store, lambda: reading)
    capacity = {(row["series"], row["subject"]): row["value"] for row in panels if row["panel"] == "capacity"}
    assert capacity == {
        ("configured_maximum", "swarm-workers"): 8.0,
        ("observed_nodes", "swarm-workers"): 2.0,
        ("active_tasks", "swarm-workers"): 1.0,
        ("pending_requests", "swarm-workers"): 1.0,
    }
    found = {row["source"]: (row["available"], row["readings"]) for row in sources}
    assert (found["pools"], found["brain_denials"]) == ((1, 1), (0, 0))


def test_an_unreadable_worker_pool_stays_an_unavailable_source(tmp_path, store):
    panels, sources = _recorded(tmp_path, store, lambda: None)
    assert not [row for row in panels if row["panel"] == "capacity"]
    assert {row["source"]: (row["available"], row["readings"]) for row in sources}["pools"] == (0, 0)


def test_checkpoint_storage_uses_the_bus_and_expires_inactive_records(monkeypatch):
    from types import SimpleNamespace

    from scripts.events import factory, memory
    from scripts.events.config import BusConfig

    bus = memory.open_bus(BusConfig())
    monkeypatch.setattr(factory, "open_bus", lambda config: bus)
    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "bus")
    monkeypatch.setenv("AGENTIHOOKS_BUS", "memory")
    clock = [1000.0]
    monkeypatch.setattr("time.time", lambda: clock[0])
    active = {"eng-1": SimpleNamespace(execution_id="first", started_at=100)}
    store = SimpleNamespace(execution_occupants=lambda slug: active)
    metrics_fleet.record_checkpoint(store, SLUG, "first", "eng-1", 200)
    metrics_fleet.record_checkpoint(store, SLUG, "first", "eng-1", 300)
    metrics_fleet.record_checkpoint(store, SLUG, "ended", "eng-9", 150)
    clock[0] += metrics_fleet.CHECKPOINT_TTL_MS / 1000 - 1
    assert metrics_fleet._checkpoints(store, SLUG) == [{"execution_id": "first", "seat": "eng-1", "taken_at_ms": 300}]
    clock[0] += 2
    assert metrics_fleet._checkpoints(store, SLUG) == [{"execution_id": "first", "seat": "eng-1", "taken_at_ms": 300}]
    saved = bus.state(metrics_fleet.CHECKPOINTS).get(SLUG)
    assert set(json.loads(saved.value)) == {"first"}
    active["eng-2"] = SimpleNamespace(execution_id="missing", started_at=400)
    assert metrics_fleet._checkpoints(store, SLUG)[1] == {
        "execution_id": "missing",
        "seat": "eng-2",
        "taken_at_ms": 400,
    }


def test_checkpoint_compare_and_swap_retries_without_losing_another_writer(monkeypatch):
    from types import SimpleNamespace

    from scripts.events import memory, state
    from scripts.events.config import BusConfig

    saved = memory.open_bus(BusConfig()).state(metrics_fleet.CHECKPOINTS)
    monkeypatch.setattr(state, "for_store", lambda *args: saved)
    metrics_fleet.record_checkpoint(SimpleNamespace(), SLUG, "first", "eng-1", 100)
    update = saved.update
    attempts = []

    def raced(key, value, revision):
        attempts.append(revision)
        if len(attempts) == 1:
            concurrent = json.loads(saved.get(key).value)
            concurrent["other"] = {"checkpoint": {"execution_id": "other"}, "expires_at_ms": 10**18}
            update(key, json.dumps(concurrent).encode(), revision)
        return update(key, value, revision)

    monkeypatch.setattr(saved, "update", raced)
    metrics_fleet.record_checkpoint(SimpleNamespace(), SLUG, "second", "eng-2", 200)
    assert len(attempts) == 2
    assert set(json.loads(saved.get(SLUG).value)) == {"first", "other", "second"}


def test_checkpoint_contention_is_reported_after_bounded_attempts(monkeypatch):
    from types import SimpleNamespace

    from scripts.events import memory, state
    from scripts.events.bus import StateConflict
    from scripts.events.config import BusConfig

    saved = memory.open_bus(BusConfig()).state(metrics_fleet.CHECKPOINTS)
    monkeypatch.setattr(state, "for_store", lambda *args: saved)
    attempts = []

    def raced(*args):
        attempts.append(args)
        raise StateConflict("taken")

    monkeypatch.setattr(saved, "create", raced)
    with pytest.raises(StateConflict, match="metrics checkpoints remained busy"):
        metrics_fleet.record_checkpoint(SimpleNamespace(), SLUG, "first", "eng-1", 100)
    assert len(attempts) == 16


def test_an_expired_checkpoint_falls_back_to_the_execution_start(monkeypatch):
    from types import SimpleNamespace

    from scripts.events import memory, state
    from scripts.events.config import BusConfig

    saved = memory.open_bus(BusConfig()).state(metrics_fleet.CHECKPOINTS)
    monkeypatch.setattr(state, "for_store", lambda *args: saved)
    clock = [1000.0]
    monkeypatch.setattr(metrics_fleet.time, "time", lambda: clock[0])
    store = SimpleNamespace(
        execution_occupants=lambda slug: {"eng-1": SimpleNamespace(execution_id="first", started_at=100)}
    )
    metrics_fleet.record_checkpoint(store, SLUG, "first", "eng-1", 200)
    clock[0] += metrics_fleet.CHECKPOINT_TTL_MS / 1000
    assert metrics_fleet._checkpoints(store, SLUG) == [{"execution_id": "first", "seat": "eng-1", "taken_at_ms": 100}]
    assert json.loads(saved.get(SLUG).value) == {}
