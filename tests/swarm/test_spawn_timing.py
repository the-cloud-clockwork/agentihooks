import json

import pytest

from scripts.swarm import capacity, preempt, profile_first, timing
from scripts.swarm import tick as tick_module
from scripts.swarm.tick import tick
from scripts.swarm_ledger import ledger_workspace
from scripts.swarm_v2.runtime import codex
from tests.swarm import test_tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime, tasks

pytestmark = pytest.mark.unit
store = test_tick.store


def name(function):
    return f"{function.__module__}.{function.__qualname__}"


def finished(capsys):
    rows = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    return [row for row in rows if row.get("event") == "swarm_tick_step" and row["phase"] == "finished"]


def test_a_spawn_pass_times_each_part_inside_the_spawn_step(store, capsys):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    with timing.tick("sw"):
        tick("sw", store, ledger, runtime, now_ms=1_000)
    rows = finished(capsys)
    steps = [row["step"] for row in rows]
    parts = [
        name(tick_module._spawn_order),
        name(capacity.read),
        name(preempt.yielding),
        name(tick_module.ready_in_order),
        name(profile_first.placeable),
        name(tick_module._lives_spent),
        name(tick_module._room_wait),
        name(tick_module._held_back),
        name(tick_module._owner_hold),
        name(ledger_workspace.scaffold),
        name(FakeLedger.update_task),
        name(tick_module.primed),
        name(FakeRuntime.spawn),
        name(codex.admit_legacy),
        name(tick_module._started),
    ]
    assert [part for part in parts if part not in steps] == []
    spawn = steps.index(name(tick_module._spawn))
    assert all(steps.index(part) < spawn for part in parts)
    assert {row["outcome"] for row in rows if row["step"] in parts} == {"success"}
    assert runtime.spawned == [("eng", "engineer@a1b2c3-0001", "t1")]


def test_a_failed_launch_times_its_settlement_inside_the_spawn_step(store, capsys):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime(fail=True)
    with timing.tick("sw"):
        tick("sw", store, ledger, runtime, now_ms=1_000)
    steps = [row["step"] for row in finished(capsys)]
    assert steps.index(name(tick_module._not_launched)) < steps.index(name(tick_module._spawn))
    assert name(tick_module._started) not in steps
    assert ledger.rows["t1"]["state"] == "open"
