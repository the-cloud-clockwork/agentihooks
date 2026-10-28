import json
import sqlite3
from contextlib import closing

import pytest

from scripts.swarm import cli, metrics, metrics_outbox
from scripts.swarm.ledger_client import LedgerGone
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from tests.inbox.test_wake import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime

NOW = 1_800_000_000_000
ON = {"AGENTIHOOKS_METRICS_URL": "http://ch:8123", "AGENTIHOOKS_METRICS_USER": "writer"}


@pytest.fixture
def spool(tmp_path, monkeypatch):
    path = tmp_path / "swarm" / "metrics-outbox.sqlite"
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: path)
    return path


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(metrics_outbox, "post", lambda sink, query, body: calls.append((sink, query, body)) or True)
    return calls


@pytest.fixture(autouse=True)
def real_ledger_record(monkeypatch):
    original = metrics.metrics_ledger.record
    monkeypatch.setattr(metrics.metrics_ledger, "record", lambda *args: None)
    return original


def test_record_pass_collects_ledger_rows_before_flushing(spool, monkeypatch, real_ledger_record):
    doc = {
        "_meta": {
            "rev": 1,
            "events": [{"rev": 1, "at": NOW - 100, "kind": "task claimed", "target": "tasks/t", "by": "worker"}],
        },
        "tasks": [{"id": "t", "lane": "ci", "state": "claimed"}],
        "time_left_minutes": 14,
    }
    acks = []

    class Ledger:
        def state(self, slug):
            assert slug == "sw"
            return doc

        def ack_events(self, slug, revision):
            acks.append((slug, revision))

    monkeypatch.setattr(metrics, "LedgerClient", Ledger)
    monkeypatch.setattr(metrics.metrics_ledger, "record", real_ledger_record)
    received = {}

    def send(sink, query, body):
        if query.startswith("INSERT"):
            received[query] = [json.loads(line) for line in body.splitlines()]
        return True

    monkeypatch.setattr(metrics_outbox, "post", send)
    assert metrics.record_pass("sw", NOW, 3, ON) == []
    assert received["INSERT INTO swarm.ledger_events FORMAT JSONEachRow"][0]["task"] == "tasks/t"
    assert received["INSERT INTO swarm.ledger_events FORMAT JSONEachRow"][0]["ts_ms"] == NOW - 100
    snapshots = received["INSERT INTO swarm.ledger_snapshots FORMAT JSONEachRow"]
    assert any(
        row["measure"] == "tasks" and row["lane"] == "ci" and row["state"] == "claimed" and row["value"] == 1
        for row in snapshots
    )
    assert received["INSERT INTO swarm.ticks FORMAT JSONEachRow"][0]["actions"] == 3
    assert acks == [("sw", 1)]


def test_record_pass_uses_its_tick_document_and_client_without_reading_a_new_revision(
    spool, sent, monkeypatch, real_ledger_record
):
    import fakeredis

    from tests.swarm.test_metrics_ledger import Ledger

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    ledger = Ledger()
    ledger.move("claimed", 1, NOW - 1)
    doc = ledger.state("sw")
    ledger.move("done", 2, NOW)
    ledger.calls.clear()
    monkeypatch.setattr(metrics, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(metrics.metrics_ledger, "record", real_ledger_record)
    swarm = metrics.metrics_swarm.TickInput(store, doc, [], lambda url: None, ledger)

    assert metrics.record_pass("sw", NOW, 0, ON, swarm) == []

    events = [
        json.loads(line)
        for _, query, body in sent
        if query == "INSERT INTO swarm.ledger_events FORMAT JSONEachRow"
        for line in body.splitlines()
    ]
    assert [(row["ledger"], row["state"], row["revision"]) for row in events] == [("sw", "claimed", 1)]
    assert ledger.calls == [("ack", "sw", 1)]


@pytest.mark.parametrize("error", [OSError, LedgerGone, SwarmError])
def test_an_unavailable_ledger_reports_the_error_and_still_flushes_ticks(spool, sent, monkeypatch, error):
    def refused(*args):
        raise error("ledger unavailable")

    monkeypatch.setattr(metrics.metrics_ledger, "record", refused)
    assert metrics.record_pass("sw", NOW, 3, ON) == ["ledger metrics failed: ledger unavailable"]
    assert sent[-1][1] == "INSERT INTO swarm.ticks FORMAT JSONEachRow"


def test_record_pass_is_off_without_settings(spool, sent):
    assert metrics.record_pass("sw", NOW, 3, {}) == []
    assert not spool.exists()
    assert sent == []


def test_record_pass_writes_a_tick_row_with_the_node_path_and_flushes(spool, sent):
    assert metrics.record_pass("sw", NOW, 3, ON) == []
    queries = [q for _, q, _ in sent]
    assert queries[0].startswith("CREATE TABLE IF NOT EXISTS swarm.ticks (")
    assert "actions Int64" in queries[0]
    assert queries[1].startswith("ALTER TABLE swarm.ticks ADD COLUMN IF NOT EXISTS event_id String")
    assert queries[2] == "INSERT INTO swarm.ticks FORMAT JSONEachRow"
    assert sent[2][0] == metrics_outbox.Settings("http://ch:8123", "writer", "")
    assert sent[2][2] == (
        b'{"event_id": "tick:sw:1800000000000", "ledger": "sw", "ts_ms": 1800000000000, '
        b'"plan": "", "phase": "", "slice": "", "task": "", "actions": 3}'
    )


def test_tick_rows_stay_local_while_the_sink_is_down(spool, monkeypatch):
    monkeypatch.setattr(metrics_outbox, "post", lambda sink, query, body: False)
    metrics.record_pass("sw", NOW, 1, ON)
    metrics.record_pass("sw", NOW + 60_000, 2, ON)
    box = metrics_outbox.Outbox(spool, metrics_outbox.settings(ON))
    assert [r["actions"] for r in box.recent("ticks", NOW + 60_000)] == [1, 2]
    box.close()


def test_a_broken_spool_is_reported_and_never_stops_the_tick(spool, sent):
    spool.parent.mkdir(parents=True)
    spool.write_text("not a database" * 100)
    result = metrics.record_pass("sw", NOW, 3, ON)
    assert len(result) == 1
    assert result[0].startswith("metrics outbox failed: ")


def test_the_spool_is_closed_after_the_pass(spool, sent, monkeypatch):
    closed = []
    real = metrics_outbox.Outbox.close
    monkeypatch.setattr(metrics_outbox.Outbox, "close", lambda self: closed.append(self) or real(self))
    metrics.record_pass("sw", NOW, 0, ON)
    assert len(closed) == 1
    with closing(sqlite3.connect(spool)) as db:
        assert db.execute("SELECT count(*) FROM spool").fetchone() == (1,)


def test_an_unwritable_swarm_home_is_reported_and_never_stops_the_tick(tmp_path, monkeypatch, sent):
    (tmp_path / "file").write_text("")
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: tmp_path / "file" / "swarm" / "outbox.sqlite")
    result = metrics.record_pass("sw", NOW, 3, ON)
    assert len(result) == 1
    assert result[0].startswith("metrics outbox failed: ")


def test_run_tick_records_metrics_after_the_priority_pass(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", ".", 0, 0, state="paused"))
    order = []
    monkeypatch.setattr(cli.dispatcher.priority_sweep, "priority_pass", lambda *args: order.append("priority") or [])
    monkeypatch.setattr(
        metrics, "record_pass", lambda slug, now, actions, env, swarm: order.append(("metrics", slug, now > 0)) or ["m"]
    )
    result = cli.run_tick(store, "sw", FakeLedger([]), FakeRuntime(), FakeHerdr({}))
    assert order == ["priority", ("metrics", "sw", True)]
    assert "m" in result


def test_a_swarm_pass_names_the_bottleneck_after_its_rows_and_ships_it(spool, sent, monkeypatch):
    import fakeredis

    from scripts.swarm import bottleneck

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    order = []
    monkeypatch.setattr(metrics.metrics_swarm, "pull_rows", lambda box, now_ms, swarm: {})
    monkeypatch.setattr(metrics.metrics_swarm, "record_pass", lambda *args: order.append("rows"))
    real = bottleneck.record
    monkeypatch.setattr(
        bottleneck,
        "record",
        lambda box, store, slug, now_ms, tasks: (
            order.append(("bottleneck", slug, now_ms, tasks)) or real(box, store, slug, now_ms, tasks)
        ),
    )
    tasks = [{"id": "t1", "state": "open"}]
    swarm = metrics.metrics_swarm.TickInput(store, {"tasks": tasks}, [], lambda url: None, None)
    assert metrics.record_pass("sw", NOW, 0, ON, swarm) == []
    assert order == ["rows", ("bottleneck", "sw", NOW, tasks)]
    assert bottleneck.read(store, "sw")["bottleneck"] == ""
    assert any(query == "INSERT INTO swarm.bottlenecks FORMAT JSONEachRow" for _, query, _ in sent)


def test_ledger_rows_are_collected_before_a_failing_swarm_collector(spool, sent, monkeypatch):
    order = []

    def broken(*args):
        order.append("swarm")
        raise ValueError("bad swarm row")

    monkeypatch.setattr(metrics.metrics_ledger, "record", lambda box, slug, now_ms, ledger: order.append("ledger"))
    monkeypatch.setattr(metrics.metrics_swarm, "pull_rows", broken)
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    swarm = metrics.metrics_swarm.TickInput(store, {"tasks": []}, [], lambda url: None, None)
    assert metrics.record_pass("sw", NOW, 0, ON, swarm) == ["metrics outbox failed: bad swarm row"]
    assert order == ["ledger", "swarm"]


def test_a_failing_collector_pass_leaves_the_interval_open_for_the_next_pass(spool, sent, monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    runs = []

    def collect(*args):
        runs.append(len(runs))
        if len(runs) == 1:
            raise ValueError("bad swarm row")

    monkeypatch.setattr(metrics, "_collect", collect)
    swarm = metrics.metrics_swarm.TickInput(store, {"tasks": []}, [], lambda url: None, None)
    assert metrics.record_pass("sw", NOW, 0, ON, swarm) == ["metrics outbox failed: bad swarm row"]
    assert metrics.record_pass("sw", NOW + 60_000, 0, ON, swarm) == []
    assert runs == [0, 1]


def test_a_reported_ledger_failure_leaves_the_interval_open_for_the_next_pass(spool, sent, monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    calls = []

    def ledger_record(*args):
        calls.append(len(calls))
        if len(calls) == 1:
            raise SwarmError("ledger unavailable")

    monkeypatch.setattr(metrics.metrics_ledger, "record", ledger_record)
    monkeypatch.setattr(metrics, "_collect", lambda *args: None)
    swarm = metrics.metrics_swarm.TickInput(store, {"tasks": []}, [], lambda url: None, None)
    assert metrics.record_pass("sw", NOW, 0, ON, swarm) == ["ledger metrics failed: ledger unavailable"]
    assert metrics.record_pass("sw", NOW + 60_000, 0, ON, swarm) == []
    assert calls == [0, 1]


def test_a_ledger_without_tasks_still_names_the_bottleneck(spool, sent, monkeypatch):
    import fakeredis

    from scripts.swarm import bottleneck

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    monkeypatch.setattr(metrics.metrics_swarm, "pull_rows", lambda box, now_ms, swarm: {})
    monkeypatch.setattr(metrics.metrics_swarm, "record_pass", lambda *args: None)
    swarm = metrics.metrics_swarm.TickInput(store, {}, [], lambda url: None, None)
    assert metrics.record_pass("sw", NOW, 0, ON, swarm) == []
    assert bottleneck.read(store, "sw")["bottleneck"] == ""


def test_a_swarm_pass_ships_the_worker_pool_reading_of_its_environment_to_the_sink(spool, sent, monkeypatch):
    import fakeredis

    from scripts.swarm_v2 import deployed

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    seen = []
    reading = [{"pool": "swarm-workers", "configured_maximum": 4, "nodes": 3}]
    monkeypatch.setattr(
        deployed, "worker_pools", lambda environ, slug: seen.append((environ, slug)) or (lambda: reading)
    )
    monkeypatch.setattr(metrics.metrics_swarm, "pull_rows", lambda box, now_ms, swarm: {})
    monkeypatch.setattr(metrics.metrics_swarm, "record_pass", lambda *args: None)
    swarm = metrics.metrics_swarm.TickInput(store, {"tasks": []}, [], lambda url: None, None)
    assert metrics.record_pass("sw", NOW, 0, ON, swarm) == []
    assert seen == [(ON, "sw")]
    shipped = [
        json.loads(line)
        for _, query, body in sent
        if query == "INSERT INTO swarm.fleet_panels FORMAT JSONEachRow"
        for line in body.decode().splitlines()
    ]
    assert {(row["series"], row["value"]) for row in shipped if row["panel"] == "capacity"} >= {
        ("configured_maximum", 4.0),
        ("observed_nodes", 3.0),
    }


def test_a_swarm_pass_ships_the_newest_checkpoint_of_an_active_execution_to_the_sink(spool, sent, monkeypatch):
    import fakeredis

    from scripts.swarm.store import AgentRecord

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    agent = AgentRecord(store.next_name("sw", "eng"), "eng", "t1", seat="eng-1@sw")
    execution = store.execution_registry.start("sw", agent, "").execution_id
    metrics.metrics_fleet.record_checkpoint(store, "sw", execution, "eng-1@sw", NOW - 2_000_000)
    monkeypatch.setattr(metrics.metrics_swarm, "pull_rows", lambda box, now_ms, swarm: {})
    monkeypatch.setattr(metrics.metrics_swarm, "record_pass", lambda *args: None)
    swarm = metrics.metrics_swarm.TickInput(store, {"tasks": []}, [], lambda url: None, None)
    assert metrics.record_pass("sw", NOW, 0, ON, swarm) == []

    def shipped(table):
        query = f"INSERT INTO swarm.{table} FORMAT JSONEachRow"
        return [json.loads(line) for _, sent_query, body in sent if sent_query == query for line in body.splitlines()]

    ages = {row["series"]: row["value"] for row in shipped("fleet_panels") if row["panel"] == "checkpoint_age"}
    assert ages == {"stale": 1.0, "oldest_age_ms": 2_000_000.0}
    sources = {row["source"]: (row["available"], row["readings"]) for row in shipped("fleet_sources")}
    assert (sources["checkpoints"], sources["pools"]) == ((1, 1), (0, 0))


def test_a_swarm_pass_measures_the_stages_of_the_ledger_tasks_and_reports_a_fleet_failure(spool, sent, monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0, template="doctor"))
    seen = []

    def broken(*args, **kwargs):
        raise SwarmError("registry down")

    monkeypatch.setattr(metrics.metrics_swarm, "pull_rows", lambda box, now_ms, swarm: {})
    monkeypatch.setattr(metrics.metrics_swarm, "record_pass", lambda *args: None)
    monkeypatch.setattr(
        metrics.stage_detector,
        "record",
        lambda box, store, slug, now_ms, tasks, environ, ready: seen.append(
            (tasks, [ready(t) for t in tasks], ready.seats)
        ),
    )
    monkeypatch.setattr(metrics.metrics_fleet, "record", broken)
    tasks = [{"id": f"t{n}", "state": "open", "phase": phase} for n, phase in ((1, "p2"), (2, "p2"), (3, "p1"))]
    doc = {"tasks": tasks, "freezes": [{"verb": "freeze", "target": "tasks/t2"}]}
    monkeypatch.setattr(
        metrics.freeze,
        "watched",
        lambda slug, store, ledger, doc: {**doc, metrics.freeze.WATCHED: ["the focus on t9 in the watched swarm"]},
    )
    swarm = metrics.metrics_swarm.TickInput(store, doc, [], lambda url: None, None)
    assert metrics.record_pass("sw", NOW, 0, ON, swarm) == ["fleet metrics failed: registry down"]
    assert seen == [(tasks, [True, False, False], {"eng": 0, "ci": 0, "plan": 1})]


@pytest.mark.parametrize("minutes,collected", [(None, [NOW, NOW + 600_000]), ("5", [NOW, NOW + 360_000])])
def test_ledger_and_swarm_collectors_run_once_per_interval_while_every_pass_writes_its_tick_row(
    spool, sent, monkeypatch, minutes, collected
):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    seen = []
    monkeypatch.setattr(metrics, "_ledger", lambda slug, now_ms, errors, box, swarm=None: seen.append(now_ms))
    monkeypatch.setattr(metrics, "_collect", lambda *args: None)
    clock = [NOW]
    monkeypatch.setattr(store.redis, "set", _expiring(store.redis, clock))
    environ = ON if minutes is None else {**ON, "AGENTIHOOKS_METRICS_EVERY_MINUTES": minutes}
    swarm = metrics.metrics_swarm.TickInput(store, {"tasks": []}, [], lambda url: None, None)
    passes = [NOW, NOW + 180_000, NOW + 360_000, NOW + 600_000]
    for at in passes:
        clock[0] = at
        assert metrics.record_pass("sw", at, 1, environ, swarm) == []

    assert seen == collected
    ticks = [
        json.loads(line)["ts_ms"]
        for _, query, body in sent
        if query == "INSERT INTO swarm.ticks FORMAT JSONEachRow"
        for line in body.splitlines()
    ]
    assert sorted(set(ticks)) == passes


def _expiring(redis, clock):
    real_set, expiry = redis.set, {}

    def set_(name, value, nx=False, px=None, **kwargs):
        if nx and redis.exists(name) and clock[0] < expiry[name]:
            return None
        if px is not None:
            expiry[name] = clock[0] + px
        return real_set(name, value, **kwargs)

    return set_


def test_a_one_millisecond_metrics_interval_is_accepted(spool, sent):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    swarm = metrics.metrics_swarm.TickInput(store, {"tasks": []}, [], lambda url: None, None)
    environ = {**ON, "AGENTIHOOKS_METRICS_EVERY_MINUTES": repr(1 / 60_000)}
    assert metrics.record_pass("sw", NOW, 1, environ, swarm) == []


@pytest.mark.parametrize("minutes", ["0", "-1", "0.00001", "inf", "nan"])
def test_a_metrics_interval_outside_positive_finite_minutes_is_reported(spool, sent, minutes):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    swarm = metrics.metrics_swarm.TickInput(store, {"tasks": []}, [], lambda url: None, None)
    environ = {**ON, "AGENTIHOOKS_METRICS_EVERY_MINUTES": minutes}
    assert metrics.record_pass("sw", NOW, 1, environ, swarm) == [
        "metrics outbox failed: metrics collectors need a positive interval in minutes"
    ]


def test_the_metrics_interval_is_claimed_on_the_bus_state_store(spool, sent, monkeypatch):
    import fakeredis

    from scripts.events import factory, memory
    from scripts.events.config import BusConfig

    bus = memory.open_bus(BusConfig())
    monkeypatch.setattr(factory, "open_bus", lambda config: bus)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    before = set(store.redis.keys("*"))
    runs = []
    monkeypatch.setattr(metrics, "_collect", lambda *args: runs.append(len(runs)))
    swarm = metrics.metrics_swarm.TickInput(store, {"tasks": []}, [], lambda url: None, None)
    environ = {**ON, "AGENTIHOOKS_STATE_BACKEND": "bus", "AGENTIHOOKS_BUS": "memory"}
    assert metrics.record_pass("sw", NOW, 0, environ, swarm) == []
    assert metrics.record_pass("sw", NOW + 60_000, 0, environ, swarm) == []
    assert metrics.record_pass("sw", NOW + 600_000, 0, environ, swarm) == []
    assert runs == [0, 1]
    assert set(store.redis.keys("*")) == before
    assert bus.state(metrics.BUCKET).get("sw") is not None


def test_a_failed_bus_pass_reopens_the_interval(spool, sent, monkeypatch):
    import fakeredis

    from scripts.events import factory, memory
    from scripts.events.config import BusConfig

    bus = memory.open_bus(BusConfig())
    monkeypatch.setattr(factory, "open_bus", lambda config: bus)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    runs = []

    def collect(*args):
        runs.append(len(runs))
        if len(runs) == 1:
            raise ValueError("bad swarm row")

    monkeypatch.setattr(metrics, "_collect", collect)
    swarm = metrics.metrics_swarm.TickInput(store, {"tasks": []}, [], lambda url: None, None)
    environ = {**ON, "AGENTIHOOKS_STATE_BACKEND": "bus", "AGENTIHOOKS_BUS": "memory"}
    assert metrics.record_pass("sw", NOW, 0, environ, swarm) == ["metrics outbox failed: bad swarm row"]
    assert metrics.record_pass("sw", NOW + 60_000, 0, environ, swarm) == []
    assert runs == [0, 1]


def test_a_metrics_interval_raced_by_another_controller_is_skipped(spool, sent, monkeypatch):
    import fakeredis

    from scripts.events.bus import StateConflict
    from scripts.events.redis_state import RedisStateStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))

    def raced(self, key, value):
        raise StateConflict("taken")

    monkeypatch.setattr(RedisStateStore, "create", raced)
    runs = []
    monkeypatch.setattr(metrics, "_collect", lambda *args: runs.append(len(runs)))
    swarm = metrics.metrics_swarm.TickInput(store, {"tasks": []}, [], lambda url: None, None)
    assert metrics.record_pass("sw", NOW, 0, ON, swarm) == []
    assert runs == []
