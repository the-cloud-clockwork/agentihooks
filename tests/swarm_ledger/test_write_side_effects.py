import threading
from unittest.mock import Mock

import pytest

from scripts.swarm_ledger import ledger_server, outbox_host
from tests.swarm_ledger.test_relay_outage import ENG, SLUG, Outage, core, make_ledger, mentions, watch_passes

ASK = {"op": "add", "thread": "chat", "id": "m-1", "text": "@ro-eng-1 please rebase onto dev"}


@pytest.fixture
def down(monkeypatch):
    return Outage(monkeypatch)


def held(rev):
    return {ledger_server.RELAY: {SLUG: [rev]}}


@pytest.mark.wall_clock
def test_a_write_returns_before_its_relay_runs(down, monkeypatch):
    make_ledger()
    release, drained = threading.Event(), threading.Event()

    def drain():
        release.wait(5)
        ledger_server.replay_unrelayed()
        drained.set()

    with outbox_host.effects(drain) as worker:
        monkeypatch.setattr(ledger_server, "EFFECTS", worker)
        state, _ = core.sync(SLUG, ops=[ASK])
        assert ledger_server.deferred(ledger_server.RELAY)(SLUG, state) == []
        assert down.texts(ENG.seat) == []
        assert ledger_server.unrelayed() == held(state["_meta"]["rev"])
        release.set()
        assert drained.wait(5)
    assert len(mentions(down.texts(ENG.seat), "please rebase onto dev")) == 1
    assert ledger_server.unrelayed() == {}


@pytest.mark.wall_clock
def test_a_failed_relay_is_retried_from_the_store_and_the_write_stands(down, monkeypatch):
    make_ledger()
    down.down()
    ran = threading.Event()

    def drain():
        ledger_server.replay_unrelayed()
        ran.set()

    with outbox_host.effects(drain) as worker:
        monkeypatch.setattr(ledger_server, "EFFECTS", worker)
        state, _ = core.sync(SLUG, ops=[ASK])
        ledger_server.deferred(ledger_server.RELAY)(SLUG, state)
        assert ran.wait(5)
        ran.clear()
        assert down.texts(ENG.seat) == []
        assert ledger_server.unrelayed() == held(state["_meta"]["rev"])
        kept, _ = core.sync(SLUG)
        assert [m["text"] for m in kept["chat"]] == [ASK["text"]]
        down.down(False)
        worker.wake()
        assert ran.wait(5)
    assert len(mentions(down.texts(ENG.seat), "please rebase onto dev")) == 1
    assert ledger_server.unrelayed() == {}


def test_a_write_with_nothing_to_relay_holds_nothing_and_wakes_no_worker(down, monkeypatch):
    make_ledger()
    worker = Mock()
    monkeypatch.setattr(ledger_server, "EFFECTS", worker)
    state, _ = core.sync(SLUG, ops=[{"op": "join", "id": "j-1", "by": "ro-eng-1"}])
    for kind in (ledger_server.RELAY, ledger_server.DOCTOR, ledger_server.ALERTS):
        assert ledger_server.deferred(kind)(SLUG, state) == []
    worker.wake.assert_not_called()
    assert ledger_server.unrelayed() == {}


def test_without_a_worker_the_side_effect_runs_in_place(monkeypatch):
    monkeypatch.setattr(ledger_server, "EFFECTS", None)
    relay = Mock(return_value=["sent"])
    monkeypatch.setattr(ledger_server, "relay_to_inbox", relay)
    state = {"_meta": {"rev": 1}}
    assert ledger_server.deferred(ledger_server.RELAY)(SLUG, state) == ["sent"]
    relay.assert_called_once_with(SLUG, state)


@pytest.mark.parametrize(
    ("kind", "state", "expected"),
    [
        (ledger_server.RELAY, {"_meta": {"rev": 3, "events": [{"rev": 3, "by": "operator"}]}}, True),
        (ledger_server.DOCTOR, {"_meta": {"rev": 3, "events": [{"rev": 3, "by": "operator"}]}}, True),
        (ledger_server.RELAY, {"_meta": {"rev": 3, "events": [{"rev": 3, "by": "ro-eng-1"}]}}, False),
        (ledger_server.RELAY, {"_meta": {"rev": 3, "events": [{"rev": 2, "by": "operator"}]}}, False),
        (ledger_server.RELAY, {"_meta": {"rev": 3}}, False),
        (ledger_server.ALERTS, {"_meta": {"rev": 3}, "alerts": [{"rev": 3, "state": "open"}]}, True),
        (ledger_server.ALERTS, {"_meta": {"rev": 3}, "alerts": [{"rev": 2, "state": "open"}]}, False),
        (ledger_server.ALERTS, {"_meta": {"rev": 3}, "alerts": [{"rev": 3, "state": "done"}]}, False),
        (ledger_server.ALERTS, {"_meta": {"rev": 3, "events": [{"rev": 3, "by": "operator"}]}}, False),
    ],
)
def test_pending_reads_only_the_work_the_write_at_its_revision_left(kind, state, expected):
    assert ledger_server.pending(kind, state) is expected


def test_page_and_api_writes_hold_every_side_effect_and_wake_the_worker(monkeypatch):
    holds, worker = [], Mock()
    monkeypatch.setattr(ledger_server, "EFFECTS", worker)
    monkeypatch.setattr(ledger_server, "unrelayed_on", lambda kind, slug, rev: holds.append((kind, slug, rev)))
    for name in ledger_server.SIDE_EFFECTS.values():
        monkeypatch.setattr(ledger_server, name, Mock(side_effect=AssertionError(name)))
    state = {
        "_meta": {"rev": 4, "warnings": [], "events": [{"rev": 4, "by": "operator"}]},
        "alerts": [{"rev": 4, "state": ledger_server.ledger_alerts.OPEN}],
    }
    monkeypatch.setattr(ledger_server, "write_gate", lambda slug: None)
    monkeypatch.setattr(
        ledger_server.repository, "apply_ops", lambda slug, changes=None, ops=None, gate=None: (state, [])
    )
    handler = ledger_server.Handler.__new__(ledger_server.Handler)
    handler.send = lambda code, body, ctype: code
    assert handler.reply_state(SLUG, changes={"x": 1}) == 200
    services = ledger_server.api_services()
    services.ledger.relay_to_inbox(SLUG, state)
    services.swarm.doctor_phrase(SLUG, state)
    services.ledger.deliver_alerts(SLUG, state)
    order = [(kind, SLUG, 4) for kind in (ledger_server.RELAY, ledger_server.DOCTOR, ledger_server.ALERTS)]
    assert holds == order * 2
    assert worker.wake.call_count == 6


def test_a_replayed_doctor_phrase_stops_the_swarm_once_after_later_writes(down, monkeypatch):
    make_ledger()
    control = Mock()
    monkeypatch.setattr(ledger_server, "swarm_control", control)
    monkeypatch.setattr(ledger_server, "EFFECTS", Mock())
    phrase = {"op": "add", "thread": "chat", "id": "m-9", "text": f" {ledger_server.DOCTOR_PHRASE.upper()} "}
    state, _ = core.sync(SLUG, ops=[phrase])
    ledger_server.deferred(ledger_server.DOCTOR)(SLUG, state)
    core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m-10", "text": "carry on"}])
    monkeypatch.setattr(ledger_server, "relay_to_inbox", Mock())
    ledger_server.replay_unrelayed()
    control.assert_called_once_with(SLUG, ["stop"], "doctor")
    assert ledger_server.unrelayed() == {}


def test_a_doctor_phrase_before_the_held_revisions_is_not_replayed(down, monkeypatch):
    make_ledger()
    control = Mock()
    monkeypatch.setattr(ledger_server, "swarm_control", control)
    early, _ = core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m-8", "text": ledger_server.DOCTOR_PHRASE}])
    late, _ = core.sync(SLUG, ops=[{"op": "add", "thread": "chat", "id": "m-9", "text": "carry on"}])
    ledger_server.unrelayed_on(ledger_server.DOCTOR, SLUG, late["_meta"]["rev"])
    ledger_server.doctor_phrase(SLUG, late)
    control.assert_not_called()
    assert early["_meta"]["rev"] < late["_meta"]["rev"]
    assert ledger_server.unrelayed() == {}


@pytest.mark.wall_clock
def test_the_effects_worker_logs_a_failed_drain_and_runs_the_next_wake(capsys):
    calls, failed, done = [], threading.Event(), threading.Event()

    def drain():
        calls.append(1)
        if len(calls) == 1:
            failed.set()
            raise OSError("redis gone")
        done.set()

    with outbox_host.effects(drain) as worker:
        worker.wake()
        assert failed.wait(5)
        worker.wake()
        assert done.wait(5)
    assert not worker.thread.is_alive()
    assert worker.thread.name == "ledger-side-effects" and worker.thread.daemon
    assert capsys.readouterr().err == "ledger side effects: redis gone\n"


def test_closing_an_idle_worker_runs_no_drain_and_ends_its_thread():
    drain = Mock()
    with outbox_host.effects(drain) as worker:
        assert worker.thread.is_alive() and not worker.stopping
    drain.assert_not_called()
    assert worker.stopping
    assert not worker.thread.is_alive()


def test_the_server_worker_drains_held_revisions_while_it_serves():
    assert ledger_server.EFFECTS is None
    with ledger_server.side_effects():
        worker = ledger_server.EFFECTS
        assert isinstance(worker, outbox_host.Effects)
        assert worker.drain is ledger_server.replay_unrelayed
        assert worker.thread.is_alive()
    assert ledger_server.EFFECTS is None
    assert not worker.thread.is_alive()


def test_the_watch_loop_wakes_the_worker_instead_of_replaying_while_one_runs(monkeypatch):
    replayed, worker = [], Mock()
    monkeypatch.setattr(ledger_server, "EFFECTS", worker)
    watch_passes(monkeypatch, held(1), replayed.append)
    assert replayed == [] and worker.wake.call_count == 2
