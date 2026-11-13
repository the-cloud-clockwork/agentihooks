import pytest

from scripts.swarm.store import MASTER
from scripts.swarm.tick import SpawnError, tick
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm.test_tick import store as store


class RoutedFailures(FakeRuntime):
    def __init__(self, *failures):
        super().__init__()
        self.failures, self.asked = list(failures), []

    def spawn(self, config, lane, name, task):
        if lane != MASTER:
            self.asked.append((name, task.get("avoid_harness", "")))
            if self.failures:
                raise self.failures.pop(0)
        return super().spawn(config, lane, name, task)


def _successor(store, *failures):
    store.put_handoff("sw", "t1", "pushed, waiting on checks")
    return tasks(("t1", "eng")), RoutedFailures(*failures)


def test_a_successor_retries_a_refused_routed_spawn_on_another_route(store):
    from scripts.swarm import failed_panes

    ledger, runtime = _successor(store, SpawnError("route refused", pane_id="w:p9", harness="codex"))
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    name = ledger.rows["t1"]["claimed_by"]
    assert failed_panes.held(store, "sw")["w:p9"]["failed_at"] == 1_000
    assert runtime.asked == [(name, ""), (name, "codex")]
    assert runtime.spawned == [("eng", name, "t1")]
    assert any(action.startswith(f"retrying {name} for t1 away from codex") for action in actions)
    assert f"spawned {name} for t1" in actions
    assert ledger.rows["t1"]["state"] == "claimed"
    assert store.launch_failure("sw", "t1") == "route refused"
    assert name in runtime.reaped


def test_two_failed_successor_spawns_retry_once_then_reopen_the_task(store):
    ledger, runtime = _successor(
        store, SpawnError("route refused", harness="codex"), SpawnError("second route refused", harness="claude")
    )
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    name = runtime.asked[0][0]
    assert runtime.asked == [(name, ""), (name, "codex")]
    assert any(action.startswith(f"retrying {name} for t1") for action in actions)
    assert any(action.startswith("spawn failed for t1, task t1 reopened") for action in actions)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("open", "")
    assert store.claimant("sw", "t1") is None
    assert [row["state"] for row in store.launches("sw")] == ["failed"]
    assert store.launch_failure("sw", "t1") == "second route refused"
    assert store.failed_harness("sw", "t1") == "claude"


@pytest.mark.parametrize(
    "handoff,error",
    [
        (False, SpawnError("route refused", harness="codex")),
        (True, SpawnError("init-agent timed out", "ambiguous", harness="codex")),
        (True, SpawnError("route refused")),
    ],
)
def test_only_a_successor_with_a_refused_routed_spawn_retries(store, handoff, error):
    if handoff:
        store.put_handoff("sw", "t1", "pushed, waiting on checks")
    ledger, runtime = tasks(("t1", "eng")), RoutedFailures(error)
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert len(runtime.asked) == 1
    assert not any(action.startswith("retrying") for action in actions)
    assert ledger.rows["t1"]["state"] == "open"


def test_steered_moves_both_avoid_fields_to_the_failed_harness_and_marks_the_retry():
    from scripts.swarm.successor_retry import RETRIED, steered

    primed = {"id": "t1", "transfer": {"avoid_harness": "codex", "reason": "exit"}}
    assert steered(primed, "claude") == {
        "id": "t1",
        "avoid_harness": "claude",
        RETRIED: True,
        "transfer": {"avoid_harness": "claude", "reason": "exit"},
    }
    assert primed["transfer"]["avoid_harness"] == "codex"
    assert steered({"id": "t1", "transfer": {}}, "codex") == {
        "id": "t1",
        "transfer": {},
        "avoid_harness": "codex",
        RETRIED: True,
    }


@pytest.mark.parametrize(
    "task,primed,error,harness",
    [
        ({"handoff": "h"}, {}, SpawnError("refused", harness="codex"), "codex"),
        ({}, {}, SpawnError("refused", harness="codex"), ""),
        ({"handoff": "h"}, {"successor_retried": True}, SpawnError("refused", harness="codex"), ""),
        ({"handoff": "h"}, {}, RuntimeError("refused"), ""),
        ({"handoff": "h"}, {}, None, ""),
        ({"handoff": "h"}, {}, SpawnError("down", "unavailable", harness="codex"), ""),
    ],
)
def test_due_names_the_harness_only_for_a_first_refused_successor_spawn(task, primed, error, harness):
    from scripts.swarm.successor_retry import due

    assert due(task, primed, error) == harness


def test_note_reaps_holds_the_failed_pane_and_records_the_failure(store):
    from scripts.swarm import failed_panes
    from scripts.swarm.store import AgentRecord
    from scripts.swarm.successor_retry import note

    record, runtime = AgentRecord("engineer@x-1", "eng", "t1"), FakeRuntime()
    said = note("sw", store, runtime, record, SpawnError("route refused", pane_id="w:p4", harness="codex"), 9)
    assert runtime.reaped == ["engineer@x-1"]
    assert said == "retrying engineer@x-1 for t1 away from codex after: route refused"
    held = {"w:p4": {"agent": "engineer@x-1", "task": "t1", "failed_at": 9, "reason": ""}}
    assert failed_panes.held(store, "sw") == held
    assert (store.launch_failure("sw", "t1"), store.failed_harness("sw", "t1")) == ("route refused", "codex")
