from dataclasses import replace
from unittest.mock import patch

import pytest

from scripts.swarm.store import MASTER
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, masters, store, tasks  # noqa: F401

pytestmark = pytest.mark.unit
DEADLINE = 120_000


@pytest.fixture(autouse=True)
def startup_inside_the_down_window(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_MASTER_DOWN_MINUTES", "60")


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
    actions = tick("sw", store, ledger, runtime, 2 * DEADLINE + 1)
    assert "master startup failed twice, raised to the operator" in actions
    alerts = [text for text in ledger.notes if "hook" in text]
    assert len(alerts) == 1
    assert alerts == [
        "The master reported no hook within two minutes on either launch. Automatic retry is exhausted; operator action is required. The original handoff is retained."
    ]
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


def test_an_unreported_launch_is_retired_with_the_master_scratch_homes(store, scratch):  # noqa: F811
    homes, runtime, ledger = scratch(MASTER), StartupRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    old = masters(store)[0]
    tick("sw", store, ledger, runtime, DEADLINE + 1)
    assert runtime.homes[old.name] == homes


def test_stuck_launch_alerts_and_keeps_original_until_retirement_succeeds(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    old = masters(store)[0]
    runtime.stuck.add(old.name)
    actions = tick("sw", store, ledger, runtime, DEADLINE + 1)
    assert len(runtime.masters) == 1
    assert actions == [f"could not retire unreported master {old.name}, retrying next tick"]
    alert = "The master reported no hook and its launch could not be retired. Operator action is required."
    assert ledger.notes.count(alert) == 1
    tick("sw", store, ledger, runtime, DEADLINE + 2)
    assert ledger.notes.count(alert) == 1
    assert len([text for text in ledger.notes if "hook" in text]) == 1
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
        actions = tick("sw", store, ledger, runtime, 2)
        assert "master spawn failed: launch refused" in actions
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
    tick("sw", store, ledger, runtime, DEADLINE + 2)
    assert not any("hook" in text for text in ledger.notes)
    tick("sw", store, ledger, runtime, 2 * DEADLINE + 1)
    assert any("operator" in text.lower() for text in ledger.notes)
    runtime.full = False
    tick("sw", store, ledger, runtime, 3 * DEADLINE)
    assert len(runtime.masters) == 1


def test_explicit_master_finish_ends_pending_startup_and_preserves_new_handoff(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    old = masters(store)[0]
    store.put_agent("sw", replace(old, state="finished"))
    store.put_handoff("sw", MASTER, "New handoff from the finished master")
    tick("sw", store, ledger, runtime, 2)
    assert len(runtime.masters) == 2
    assert runtime.masters[-1][1]["handoff"] == "New handoff from the finished master"


def test_retry_checks_the_replacement_hook_before_another_launch(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    tick("sw", store, ledger, runtime, DEADLINE + 1)
    runtime.registered.add(runtime.masters[-1][0])
    tick("sw", store, ledger, runtime, DEADLINE + 2)
    tick("sw", store, ledger, runtime, 3 * DEADLINE)
    assert len(runtime.masters) == 2
    assert not any("hook" in text for text in ledger.notes)


def test_pending_startup_is_owned_by_the_deadline_check(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    runtime.bindings = lambda agents: {a.name: {"process": False} for a in agents}
    tick("sw", store, ledger, runtime, 2)
    assert len(runtime.masters) == 1
    assert runtime.killed == []
    assert runtime.named == []


def test_master_awaiting_a_restore_decision_is_not_verified_or_reaped(store):  # noqa: F811
    from scripts.swarm.store import AgentRecord

    runtime, ledger = StartupRuntime(), tasks()
    store.put_agent("sw", AgentRecord("master@a1b2c3-0099", MASTER, MASTER, state="awaiting-decision"))
    runtime.bindings = lambda agents: {a.name: {"process": False} for a in agents}
    tick("sw", store, ledger, runtime, 600_000)
    assert masters(store)[0].state == "awaiting-decision"
    assert runtime.masters == []
    assert runtime.closed == []


def test_stale_finished_master_does_not_cancel_the_new_startup(store):  # noqa: F811
    from scripts.swarm.store import AgentRecord

    runtime, ledger = StartupRuntime(), tasks()
    tick("sw", store, ledger, runtime, 1)
    old = AgentRecord("master@a1b2c3-0099", MASTER, MASTER, state="finished")
    store.put_agent("sw", old)
    runtime.stuck.add(old.name)
    tick("sw", store, ledger, runtime, 2)
    tick("sw", store, ledger, runtime, DEADLINE + 1)
    assert len(runtime.masters) == 2


def test_missing_startup_record_still_retries_with_the_original_handoff(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    store.put_handoff("sw", MASTER, "Original handoff")
    tick("sw", store, ledger, runtime, 1)
    store.drop_agent("sw", runtime.masters[-1][0])
    runtime.live.clear()
    actions = tick("sw", store, ledger, runtime, DEADLINE + 1)
    assert "master launch failed, retrying once" in actions
    assert len(runtime.masters) == 2
    assert runtime.masters[-1][1]["handoff"] == "Original handoff"


def test_failed_spawn_waiting_for_capacity_alerts_without_resetting_its_deadline(store):  # noqa: F811
    runtime, ledger = StartupRuntime(), tasks()
    runtime.crash = RuntimeError("launch refused")
    tick("sw", store, ledger, runtime, 1)
    runtime.full = True
    tick("sw", store, ledger, runtime, 2)
    actions = tick("sw", store, ledger, runtime, DEADLINE + 1)
    assert "master startup failed twice, raised to the operator" in actions
    assert any("operator" in text.lower() for text in ledger.notes)


def test_synchronously_registered_master_is_working_and_clears_the_handoff(store):  # noqa: F811
    runtime, ledger = FakeRuntime(), tasks()
    store.put_handoff("sw", MASTER, "Original handoff")
    tick("sw", store, ledger, runtime, 1)
    assert masters(store)[0].state == "working"
    assert store.handoff("sw", MASTER) == ""


def test_the_alert_state_is_saved_before_the_notice(store):  # noqa: F811
    from scripts.swarm import master_start
    from scripts.swarm.store import SwarmError

    class Raising:
        def notify(self, slug, text):
            raise SwarmError("ledger sw: connection reset")

    master_start.save(store, "sw", {"name": "", "task": {}, "attempt": 2, "retry": True, "at": 0})
    with pytest.raises(SwarmError):
        master_start.observe("sw", store.config("sw"), store, Raising(), StartupRuntime(), DEADLINE)
    assert master_start.read(store, "sw")["alerted"] is True
