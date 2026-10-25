import json

import fakeredis
import pytest

from scripts.hive import registry
from scripts.swarm import ledger_probe, metrics_outbox, metrics_service
from scripts.swarm.store import RedisStore, SwarmConfig

NOW = 1_800_000_000_000


@pytest.fixture
def store():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    return store


def _probe(store, **sample):
    row = {"read_s": 0.25, "write_s": 1.5, "read_failure": "", "write_failure": "", "wall_s": 1.75, "at": NOW}
    store.redis.set(ledger_probe._key(store, "sw"), json.dumps({"samples": [{"at": NOW - 60_000}, {**row, **sample}]}))


def test_the_last_probe_pass_becomes_one_ledger_probe_row(store):
    _probe(store, write_failure=ledger_probe.TIMED_OUT)

    rows = metrics_service.probe_rows(store, "sw")

    assert rows == [
        {
            "event_id": f"probe:sw:{NOW}",
            "ledger": "sw",
            "ts_ms": NOW,
            "plan": "",
            "phase": "",
            "slice": "",
            "task": "",
            "read_s": 0.25,
            "write_s": 1.5,
            "read_failure": "",
            "write_failure": "timed out",
            "wall_s": 1.75,
        }
    ]
    metrics_service.PROBES.check(rows[0])


def test_no_probe_pass_yet_gives_no_row(store):
    assert metrics_service.probe_rows(store, "sw") == []


def test_each_hive_becomes_a_row_with_its_live_flag_and_heartbeat_age(store):
    registry.update(store.redis, "h1", ["name=anton-1"])
    registry.update(store.redis, "h2", ["name=anton-2"])
    store.redis.hset(registry.key("h1"), "heartbeat_at", json.dumps(NOW - 30_000))
    store.redis.hset(registry.key("h2"), "heartbeat_at", json.dumps(NOW - registry.LIVE_MS - 1))

    rows = metrics_service.hive_rows(store, "sw", NOW)

    assert [(r["event_id"], r["hive"], r["name"], r["live"], r["heartbeat_age_s"]) for r in rows] == [
        (f"hive:sw:h1:{NOW}", "h1", "anton-1", 1, 30.0),
        (f"hive:sw:h2:{NOW}", "h2", "anton-2", 0, (registry.LIVE_MS + 1) / 1000),
    ]
    for row in rows:
        metrics_service.HIVES.check(row)


def test_a_hive_that_never_beat_reads_age_minus_one(store):
    registry.update(store.redis, "h1", ["name=anton-1"])

    (row,) = metrics_service.hive_rows(store, "sw", NOW)

    assert (row["live"], row["heartbeat_age_s"]) == (0, -1.0)


def test_record_appends_probe_and_hive_rows_to_the_outbox(store, tmp_path):
    _probe(store)
    registry.update(store.redis, "h1", ["name=anton-1"])
    box = metrics_outbox.Outbox(tmp_path / "spool.sqlite", None, send=lambda *args: True)
    try:
        metrics_service.record(box, store, "sw", NOW)
        tables = sorted(row[0] for row in box.db.execute("SELECT tbl FROM spool"))
    finally:
        box.close()

    assert tables == ["hive_samples", "ledger_probes"]
