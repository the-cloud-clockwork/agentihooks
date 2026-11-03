import json
import os
import subprocess
import sys

import fakeredis
import pytest

from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_master_recovery import GRACE_MS, NOW, RecoveryRuntime, _hive, _hive_master
from tests.swarm.test_tick import tasks


def _scenario() -> tuple[list, list]:
    import sys as _sys
    import traceback

    from scripts.hive import registry
    from scripts.swarm import affinity, master_move
    from scripts.tick import engine
    from tests.hive.test_controller_launch import publish_room

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    _hive(store, "lost", NOW - GRACE_MS - 1)
    for hive, room, workers in (("big", 0, 8), ("small", 2, 6)):
        _hive(store, hive, NOW)
        registry.beat(store.redis, hive, {"max_agents": 8})
        store.redis.set(f"{registry.key(hive)}:sessions", json.dumps({"claude:one": workers}))
        publish_room(store, hive, room)
    _hive_master(store)
    runtime = RecoveryRuntime()
    records: list = []
    seen: list = []
    original_step, original_fail = engine.run_step, master_move.fail_over

    def step(step: object, ctx: object) -> object:
        record = original_step(step, ctx)
        if record.held or record.abandoned or record.error:
            records.append(str(record))
        return record

    def fail(slug: str, config: object, store_: object, runtime_: object, now_ms: int) -> list:
        master = affinity.live_master(store_, slug)
        quiet = master_move.quiet_for(store_, config, master, now_ms) if master else None
        live = sorted(runtime_.live_names())
        found = original_fail(slug, config, store_, runtime_, now_ms)
        seen.append({"master": getattr(master, "name", None), "quiet": quiet, "live": live, "found": found})
        return found

    engine.run_step, master_move.fail_over = step, fail
    try:
        actions = tick("sw", store, tasks(), runtime, NOW)
    finally:
        engine.run_step, master_move.fail_over = original_step, original_fail
    if not runtime.masters:
        frames = _sys._current_frames()
        alive = {
            name: "".join(traceback.format_stack(frames[worker.ident])[-4:])
            for name, worker in engine._RUNNING.items()
            if worker.is_alive() and worker.ident in frames
        }
        actions = [*actions, {"records": records, "fail_over": seen, "alive": alive}]
    return actions, runtime.masters


def test_probe_in_process() -> None:
    bad = []
    for attempt in range(300):
        actions, launched = _scenario()
        if [task.get("hive") for _, task in launched] != ["small"]:
            bad.append((attempt, actions, [t.get("hive") for _, t in launched]))
    assert not bad, json.dumps([len(bad), bad[:2]], default=str)


def test_probe_once() -> None:
    actions, launched = _scenario()
    assert [task.get("hive") for _, task in launched] == ["small"], json.dumps(actions, default=str)


@pytest.mark.parametrize("attempt", range(40))
def test_probe_cold_process(attempt: int) -> None:
    if os.environ.get("PROBE_CHILD"):
        pytest.skip("child")
    node = "tests/swarm/test_master_recovery_probe.py::test_probe_once"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x", "--tb=short", node]
    done = subprocess.run(command, capture_output=True, text=True, env={**os.environ, "PROBE_CHILD": "1"})
    assert done.returncode == 0, done.stdout[-4000:]


PREDECESSORS = (
    "tests/swarm/test_metrics_outbox.py::test_metric_history_survives_a_read_lock_longer_than_the_sqlite_default",
    "tests/swarm/test_command_runner.py::test_tick_executes_queued_control_then_publishes_its_result",
    "tests/swarm/test_quarantine_priming.py::test_learned_refuses_a_note_restating_an_open_correction",
    "tests/swarm_ledger/test_task_group.py::test_the_ledger_groups_two_medium_tasks_under_the_fold_ceiling",
    "tests/swarm_ledger/test_task_group.py::test_ungroup_leaves_a_member_that_points_at_another_lead",
    "tests/swarm/test_master_recovery.py::test_the_lost_hive_move_places_the_master_on_the_hive_with_room",
)


@pytest.mark.parametrize("attempt", range(8))
def test_probe_after_predecessors(attempt: int) -> None:
    if os.environ.get("PROBE_CHILD"):
        pytest.skip("child")
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short", *PREDECESSORS]
    done = subprocess.run(command, capture_output=True, text=True, env={**os.environ, "PROBE_CHILD": "1"})
    assert done.returncode == 0, done.stdout[-4000:]


@pytest.mark.parametrize("attempt", range(8))
def test_probe_target_alone(attempt: int) -> None:
    if os.environ.get("PROBE_CHILD"):
        pytest.skip("child")
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short", PREDECESSORS[-1]]
    done = subprocess.run(command, capture_output=True, text=True, env={**os.environ, "PROBE_CHILD": "1"})
    assert done.returncode == 0, done.stdout[-4000:]
