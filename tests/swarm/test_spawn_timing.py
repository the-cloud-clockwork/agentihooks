import json

import pytest

from scripts.handoff import transfers
from scripts.swarm import capacity, dispatcher, preempt, profile_first, timing
from scripts.swarm import tick as tick_module
from scripts.swarm.store import RedisStore
from scripts.swarm.tick import tick
from scripts.swarm_ledger import ledger_workspace
from scripts.swarm_v2.runtime import codex
from tests.swarm import test_tick

pytestmark = pytest.mark.unit
store = test_tick.store
FakeLedger, FakeRuntime, tasks = test_tick.FakeLedger, test_tick.FakeRuntime, test_tick.tasks


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
        timing.named(tick_module._held_for_master),
        timing.named(tick_module._spawn_order),
        timing.named(capacity.read),
        timing.named(capacity.caps),
        timing.named(dispatcher.claim_key),
        timing.named(preempt.yielding),
        timing.named(tick_module.ready_in_order),
        timing.named(profile_first.placeable),
        timing.named(tick_module._ranked),
        timing.named(tick_module._block_unresolved),
        timing.named(tick_module._spawn_stop),
        timing.named(tick_module._lives_spent),
        timing.named(tick_module._room_wait),
        timing.named(tick_module._held_back),
        timing.named(tick_module._owner_hold),
        timing.named(RedisStore.claim),
        timing.named(tick_module._reported),
        timing.named(tick_module._sharing),
        timing.named(ledger_workspace.scaffold),
        timing.named(FakeLedger.update_task),
        timing.named(RedisStore.record_launch),
        timing.named(transfers.attach),
        timing.named(tick_module.primed),
        timing.named(FakeRuntime.spawn),
        timing.named(codex.admit_legacy),
        timing.named(tick_module._started),
    ]
    assert [part for part in parts if part not in steps] == []
    spawn = steps.index(timing.named(tick_module._spawn))
    assert all(steps.index(part) < spawn for part in parts)
    assert {row["outcome"] for row in rows if row["step"] in parts} == {"success"}
    assert runtime.spawned == [("eng", "engineer@a1b2c3-0001", "t1")]


def test_a_failed_launch_times_its_settlement_inside_the_spawn_step(store, capsys):
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime(fail=True)
    with timing.tick("sw"):
        tick("sw", store, ledger, runtime, now_ms=1_000)
    steps = [row["step"] for row in finished(capsys)]
    assert steps.index(timing.named(tick_module._not_launched)) < steps.index(timing.named(tick_module._spawn))
    assert timing.named(tick_module._started) not in steps
    assert ledger.rows["t1"]["state"] == "open"


def test_a_failed_launch_reports_its_failure_once_under_the_spawn_step(store, capsys):
    ledger, runtime, reported = tasks(("t1", "eng")), FakeRuntime(fail=True), []
    token = timing.ON_FAILURE.set(lambda step, error: reported.append((step, str(error))))
    try:
        with timing.tick("sw"):
            tick("sw", store, ledger, runtime, now_ms=1_000)
    finally:
        timing.ON_FAILURE.reset(token)
    assert [entry for entry in reported if entry[1] == "herdr down"] == [("scripts.swarm.tick._spawn", "herdr down")]
    (launch,) = [row for row in finished(capsys) if row["step"] == timing.named(FakeRuntime.spawn)]
    assert (launch["outcome"], launch["error"]) == ("error", "scripts.swarm.tick.SpawnError")
