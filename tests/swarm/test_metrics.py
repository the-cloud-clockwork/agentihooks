import json
import sqlite3
from contextlib import closing

import pytest

from scripts.swarm import cli, metrics, metrics_outbox
from scripts.swarm.ledger_client import LedgerGone
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from tests.inbox.test_wake import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")

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
    swarm = metrics.metrics_swarm.TickInput(store, {"tasks": tasks}, [], lambda url: None)
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
    swarm = metrics.metrics_swarm.TickInput(None, {"tasks": []}, [], lambda url: None)
    assert metrics.record_pass("sw", NOW, 0, ON, swarm) == ["metrics outbox failed: bad swarm row"]
    assert order == ["ledger", "swarm"]


def test_a_ledger_without_tasks_still_names_the_bottleneck(spool, sent, monkeypatch):
    import fakeredis

    from scripts.swarm import bottleneck

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 0, 0))
    monkeypatch.setattr(metrics.metrics_swarm, "pull_rows", lambda box, now_ms, swarm: {})
    monkeypatch.setattr(metrics.metrics_swarm, "record_pass", lambda *args: None)
    swarm = metrics.metrics_swarm.TickInput(store, {}, [], lambda url: None)
    assert metrics.record_pass("sw", NOW, 0, ON, swarm) == []
    assert bottleneck.read(store, "sw")["bottleneck"] == ""
