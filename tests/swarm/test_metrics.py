import sqlite3
from contextlib import closing

import fakeredis
import pytest

from scripts.swarm import cli, metrics, metrics_outbox
from scripts.swarm.store import RedisStore, SwarmConfig
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
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", ".", 0, 0, state="paused"))
    order = []
    monkeypatch.setattr(cli.priority_sweep, "priority_pass", lambda *args: order.append("priority") or [])
    monkeypatch.setattr(
        metrics, "record_pass", lambda slug, now, actions, env, swarm: order.append(("metrics", slug, now > 0)) or ["m"]
    )
    result = cli.run_tick(store, "sw", FakeLedger([]), FakeRuntime(), FakeHerdr({}))
    assert order == ["priority", ("metrics", "sw", True)]
    assert "m" in result
