from dataclasses import replace
from unittest.mock import patch

import pytest

from scripts.swarm.store import MASTER
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks  # noqa: F401

pytestmark = pytest.mark.unit
DEADLINE = 120_000


class StartupRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.registered = set()

    def reported(self, agent):
        return agent.name in self.registered


def test_unreported_master_retries_original_handoff_then_alerts_once(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    store.put_handoff("sw", MASTER, "Original complete handoff")
    tick("sw", store, ledger, runtime, 1)
    first, original = runtime.masters[0]
    tick("sw", store, ledger, runtime, DEADLINE)
    assert len(runtime.masters) == 1
    tick("sw", store, ledger, runtime, DEADLINE + 1)
    assert len(runtime.masters) == 2
    assert first in runtime.killed
    assert runtime.masters[1][1] == original
    tick("sw", store, ledger, runtime, 2 * DEADLINE + 1)
    alerts = [text for text in ledger.notes if "hook" in text]
    assert len(alerts) == 1
    assert "operator" in alerts[0].lower()
    tick("sw", store, ledger, runtime, 10 * DEADLINE)
    assert len(runtime.masters) == 2
    assert [text for text in ledger.notes if "hook" in text] == alerts
    assert store.handoff("sw", MASTER) == "Original complete handoff"


def test_first_hook_confirms_startup_and_prevents_retry(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    store.put_handoff("sw", MASTER, "Original handoff")
    tick("sw", store, ledger, runtime, 1)
    master = masters(store)[0]
    assert master.state == "starting"
    runtime.registered.add(master.name)
    tick("sw", store, ledger, runtime, 2)
    assert masters(store)[0].state == "working"
    assert store.handoff("sw", MASTER) == ""
    assert store.redis.get(store.key("sw", "master-start")) is None
    tick("sw", store, ledger, runtime, 3 * DEADLINE)
    assert len(runtime.masters) == 1


def test_dead_launch_retries_without_waiting_for_worker_grace(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    store.put_handoff("sw", MASTER, "Retain this handoff")
    tick("sw", store, ledger, runtime, 1)
    runtime.live.clear()
    tick("sw", store, ledger, runtime, DEADLINE + 1)
    assert len(runtime.masters) == 2
    assert runtime.masters[-1][1]["handoff"] == "Retain this handoff"


def test_stuck_launch_alerts_and_keeps_original_until_retirement_succeeds(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    old = masters(store)[0]
    runtime.stuck.add(old.name)
    tick("sw", store, ledger, runtime, DEADLINE + 1)
    assert len(runtime.masters) == 1
    assert any("operator" in text.lower() for text in ledger.notes)
    runtime.stuck.clear()
    tick("sw", store, ledger, runtime, DEADLINE + 2)
    assert len(runtime.masters) == 2


def test_stopping_cancels_startup_without_relaunch(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    store.update("sw", state="stopping")
    tick("sw", store, ledger, runtime, DEADLINE + 1)
    assert len(runtime.masters) == 1
    assert store.redis.get(store.key("sw", "master-start")) is None


def test_startup_spawn_exceptions_are_bounded(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    runtime.crash = RuntimeError("launch refused")
    with patch.object(runtime, "spawn", wraps=runtime.spawn) as spawn:
        tick("sw", store, ledger, runtime, 1)
        tick("sw", store, ledger, runtime, 2)
        tick("sw", store, ledger, runtime, DEADLINE + 1)
        tick("sw", store, ledger, runtime, 2 * DEADLINE + 1)
        tick("sw", store, ledger, runtime, 10 * DEADLINE)
    assert spawn.call_count == 2
    assert len([text for text in ledger.notes if "hook" in text]) == 1


def test_manual_master_replacement_cancels_old_startup(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    old = masters(store)[0]
    store.drop_agent("sw", old.name)
    runtime.live.discard(old.name)
    new = replace(old, name="master@a1b2c3-0099", state="working")
    store.put_agent("sw", new)
    runtime.live.add(new.name)
    runtime.registered.add(new.name)
    tick("sw", store, ledger, runtime, DEADLINE + 1)
    assert len(runtime.masters) == 1
    assert masters(store)[0].name == new.name
    assert store.redis.get(store.key("sw", "master-start")) is None


@pytest.mark.parametrize(("status", "reported"), [("unregistered", False), ("alive", True)])
def test_runtime_distinguishes_hook_registration_from_process_configuration(monkeypatch, status, reported):
    from types import SimpleNamespace

    from scripts import terminate_agent
    from scripts.swarm import live_binding
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import AgentRecord

    agent = AgentRecord("master@a1b2c3-0001", MASTER, MASTER)
    session = SimpleNamespace(status=status, process=SimpleNamespace(pid=1234))
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [session])
    monkeypatch.setattr(live_binding, "bound_session", lambda agent, items: session)
    monkeypatch.setattr(live_binding, "read", lambda agent, pid: {"hooks": True})
    assert HerdrRuntime().reported(agent) is reported


def test_retry_attaches_original_handoff_transfer_to_the_new_master(store):  # noqa: F811
    from scripts.handoff import transfers
    from scripts.swarm.store import AgentRecord

    old = AgentRecord("master@a1b2c3-0098", MASTER, MASTER, state="finished", seat="master@sw")
    store.put_agent("sw", old)
    original = transfers.record(store, "sw", old, "recycle", "Full handoff", 0)
    store.put_handoff("sw", MASTER, "Full handoff", seat=old.seat)
    runtime, ledger = StartupRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    first = runtime.masters[-1][1]["transfer"]
    assert first["id"] == original["id"]
    assert transfers.get(store, "sw", first["id"])["binding"]["state"] != "live"
    tick("sw", store, ledger, runtime, DEADLINE + 1)
    retry = runtime.masters[-1][1]["transfer"]
    assert retry["retry_of"] == original["id"]
    assert retry["handoff"] == first["handoff"]
    assert retry["successor"] == masters(store)[0].name
    runtime.registered.add(retry["successor"])
    tick("sw", store, ledger, runtime, DEADLINE + 2)
    assert transfers.get(store, "sw", retry["id"])["binding"]["state"] == "live"


def test_retry_capacity_failure_raises_within_second_deadline(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    runtime.full = True
    tick("sw", store, ledger, runtime, DEADLINE + 1)
    tick("sw", store, ledger, runtime, 2 * DEADLINE + 1)
    assert any("operator" in text.lower() for text in ledger.notes)
    runtime.full = False
    tick("sw", store, ledger, runtime, 3 * DEADLINE)
    assert len(runtime.masters) == 1
