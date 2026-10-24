from unittest.mock import patch

import pytest

from scripts.swarm import master_start
from scripts.swarm.store import MASTER
from scripts.swarm.tick import tick
from tests.swarm.test_master_start import StartupRuntime, startup_inside_the_down_window  # noqa: F401
from tests.swarm.test_tick import store, tasks  # noqa: F401

pytestmark = pytest.mark.unit


def test_alerted_master_relaunches_with_original_handoff_within_five_minutes(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    runtime.crash = RuntimeError("no master claude ai login")
    store.put_handoff("sw", MASTER, "Original complete handoff")
    tick("sw", store, ledger, runtime, 1)
    tick("sw", store, ledger, runtime, 2)
    tick("sw", store, ledger, runtime, 120_002)
    pending = master_start.read(store, "sw")
    assert pending["alerted"] is True
    original = pending["task"]
    runtime.crash = None
    with patch.object(runtime, "spawn", wraps=runtime.spawn) as spawn:
        tick("sw", store, ledger, runtime, 420_001)
        assert spawn.call_count == 0
        tick("sw", store, ledger, runtime, 420_002)
    assert len(runtime.masters) == 1, "master seat remains empty 5 minutes after login recovery"
    name, task = runtime.masters[0]
    assert task["handoff"] == original["handoff"]
    assert task["transfer"] == original["transfer"]
    assert store.handoff("sw", MASTER) == "Original complete handoff"
    runtime.registered.add(name)
    tick("sw", store, ledger, runtime, 420_003)
    assert master_start.read(store, "sw") == {}
    assert store.handoff("sw", MASTER) == ""


@pytest.mark.parametrize("error", ["no master claude ai login", "route unavailable"])
def test_alerted_failure_keeps_one_notice_and_waits_five_minutes_between_attempts(store, error):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    runtime.crash = RuntimeError(error)
    tick("sw", store, ledger, runtime, 1)
    tick("sw", store, ledger, runtime, 2)
    tick("sw", store, ledger, runtime, 120_002)
    notices = list(ledger.notes)
    with patch.object(runtime, "spawn", wraps=runtime.spawn) as spawn:
        actions = tick("sw", store, ledger, runtime, 420_002)
        assert spawn.call_count == 1
        assert f"master spawn failed: {error}" not in actions
        assert master_start.read(store, "sw")["alerted"] is True
        tick("sw", store, ledger, runtime, 720_001)
        assert spawn.call_count == 1
        tick("sw", store, ledger, runtime, 720_002)
        assert spawn.call_count == 2
    assert ledger.notes == notices
    from scripts.swarm import master_alarm

    assert master_alarm.read(store, "sw")["error"] == f"master spawn failed: {error}"


@pytest.mark.parametrize("name", ["", "master@a1b2c3-0098"])
def test_legacy_alerted_record_rechecks_capacity_and_relaunches_at_the_deadline(store, name):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    original = {"id": MASTER, "handoff": "Retained handoff"}
    master_start.save(store, "sw", {"name": name, "task": original, "attempt": 2, "at": 0, "alerted": True})
    runtime.full = True
    tick("sw", store, ledger, runtime, 300_000)
    assert not runtime.masters
    assert master_start.read(store, "sw")["alerted"] is True
    runtime.full = False
    tick("sw", store, ledger, runtime, 300_001)
    assert len(runtime.masters) == 1
    assert runtime.masters[0][1]["handoff"] == "Retained handoff"


def test_unreported_recovery_is_retired_and_retried_without_another_notice(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    tick("sw", store, ledger, runtime, 120_001)
    tick("sw", store, ledger, runtime, 240_001)
    notices = list(ledger.notes)
    tick("sw", store, ledger, runtime, 540_001)
    assert len(runtime.masters) == 3
    name = runtime.masters[-1][0]
    tick("sw", store, ledger, runtime, 660_001)
    assert name in runtime.killed
    assert master_start.read(store, "sw")["alerted"] is True
    assert ledger.notes == notices
    tick("sw", store, ledger, runtime, 960_000)
    assert len(runtime.masters) == 3
    tick("sw", store, ledger, runtime, 960_001)
    assert len(runtime.masters) == 4


def test_alerted_relaunch_attaches_the_retained_transfer_to_its_new_master(store):  # noqa: F811
    from scripts.handoff import transfers
    from scripts.swarm.store import AgentRecord

    old = AgentRecord("master@a1b2c3-0098", MASTER, MASTER, state="finished", seat="master@sw")
    store.put_agent("sw", old)
    transfer = transfers.record(store, "sw", old, "recycle", "Full handoff", 0)
    store.put_handoff("sw", MASTER, "Full handoff", seat=old.seat)
    runtime, ledger = StartupRuntime(), tasks()
    runtime.crash = RuntimeError("no master claude ai login")
    tick("sw", store, ledger, runtime, 1)
    tick("sw", store, ledger, runtime, 2)
    tick("sw", store, ledger, runtime, 120_002)
    runtime.crash = None
    tick("sw", store, ledger, runtime, 420_002)
    assert len(runtime.masters) == 1
    name, task = runtime.masters[0]
    retry = task["transfer"]
    previous = transfers.get(store, "sw", retry["retry_of"])
    assert previous["retry_of"] == transfer["id"]
    assert retry["handoff"] == "Full handoff"
    assert retry["successor"] == name
    runtime.registered.add(name)
    tick("sw", store, ledger, runtime, 420_003)
    assert transfers.get(store, "sw", retry["id"])["binding"]["state"] == "live"


def test_forced_master_recovery_preserves_the_alert_and_its_backoff(store, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENTIHOOKS_MASTER_DOWN_MINUTES", "5")
    runtime, ledger = StartupRuntime(), tasks()
    runtime.crash = RuntimeError("no master claude ai login")
    tick("sw", store, ledger, runtime, 1)
    tick("sw", store, ledger, runtime, 2)
    tick("sw", store, ledger, runtime, 120_002)
    pending = master_start.read(store, "sw")
    with patch.object(runtime, "spawn", wraps=runtime.spawn) as spawn:
        tick("sw", store, ledger, runtime, 300_001)
        assert master_start.read(store, "sw") == pending
        assert spawn.call_count == 0
        actions = tick("sw", store, ledger, runtime, 420_002)
        assert spawn.call_count == 1
        assert "master spawn failed: no master claude ai login" not in actions
    assert master_start.read(store, "sw")["alerted"] is True
    assert len([note for note in ledger.notes if "reported no hook" in note]) == 1


def test_forced_recovery_replaces_an_unalerted_stale_start(store, monkeypatch):  # noqa: F811
    from scripts.swarm import tick_master
    from scripts.swarm.tick import _master
    from tests.swarm.test_tick_master import direct

    monkeypatch.setenv("AGENTIHOOKS_MASTER_DOWN_MINUTES", "5")
    swarm, runtime = direct()
    master_start.save(swarm, "sw", {"name": "master@a1b2c3-0098", "task": {"id": MASTER}, "attempt": 1, "at": 0})
    config, ledger = swarm.config("sw"), tasks()
    tick_master.run(
        "sw", config, swarm, ledger, runtime, 300_001, lambda: _master("sw", config, swarm, runtime, 300_001)
    )
    assert len(runtime.masters) == 1
    assert runtime.masters[0][0] != "master@a1b2c3-0098"
    assert master_start.read(swarm, "sw") == {}
