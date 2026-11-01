"""Declared step needs, reads and writes keep today's tick constraints with no hand written order."""

from pathlib import Path

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.tick import engine, registry
from scripts.tick.steps import AGENTS, SWARM, TASKS, actions_of, budgets
from tests.swarm.tick_scenarios.test_characterisation import replay

TODAY = {
    "start": ["config load", "stopped branch"],
    "observe": [
        "pane evidence",
        "execution reconcile",
        "master move fail over",
        "recover master",
        "ledger state",
        "automated cleanup",
        "spawn due",
        "exits sweep",
        "master start watch",
        "launch checks",
        "track outages",
        "verify",
    ],
    "agents": ["reap", "recover outages", "strays", "failed panes"],
    "tasks": ["orphans"],
    "task passes": ["run window", "verification notices", "size pass", "grouping release", "dispatcher group"],
    "placement acks": ["collect acks", "codex hook order"],
    "placement": ["rank pass", "bottleneck remedy", "preempt", "spawn"],
    "close": ["conversations", "session models", "handoff transfers", "settle", "close space"],
    "wrapper events": [
        "ledger event pass",
        "done recheck",
        "progress checks",
        "waits end",
        "merge pass",
        "stall watch",
        "overrun pass",
        "quiet pass",
        "doc report",
        "priorities sweep",
        "health findings",
        "findings pass",
    ],
    "wrapper metrics": ["metrics record", "bottleneck status", "inbox wake", "snapshot job", "last tick"],
}
EDGES = (
    ("reap", "orphans"),
    ("orphans", "spawn"),
    ("reap", "preempt"),
    ("preempt", "spawn"),
    ("spawn", "settle"),
    ("stall watch", "overrun pass"),
    ("health findings", "findings pass"),
    ("health findings", "metrics record"),
)


def wave_of() -> dict[str, int]:
    return {step.name: index for index, wave in enumerate(registry.waves()) for step in wave}


def declared(name: str) -> tuple[frozenset[str], frozenset[str]]:
    step = next(step for step in registry.steps() if step.name == name)
    return step.reads, step.writes


@pytest.mark.feature("swarm-tick-declared-edges")
def test_registry_waves_reproduce_todays_constraints() -> None:
    waves = wave_of()
    for before, after in (*EDGES, *((need, "spawn") for need in sorted(budgets.SPAWN_NEEDS))):
        assert waves[before] < waves[after], (before, after)
    assert waves["settle"] == max(waves.values())
    assert [name for name, wave in waves.items() if wave == max(waves.values())] == ["settle"]


@pytest.mark.feature("swarm-tick-declared-edges")
def test_every_group_runs_in_todays_order_from_needs_and_registration() -> None:
    wave_of()
    for group, names in TODAY.items():
        assert [step.name for step in engine.group_steps(group)] == names, group
    assert all(not hasattr(step, "order") for step in registry.steps())


@pytest.mark.feature("swarm-tick-declared-edges")
def test_the_steps_on_each_edge_declare_what_they_read_and_write() -> None:
    wave_of()
    assert declared("reap") == (frozenset({AGENTS}), frozenset({AGENTS, TASKS, actions_of("reap")}))
    assert declared("orphans") == (frozenset({TASKS}), frozenset({TASKS, actions_of("orphans")}))
    assert declared("preempt") == (frozenset({AGENTS}), frozenset({AGENTS, actions_of("preempt")}))
    assert declared("spawn") == (frozenset({AGENTS, TASKS, SWARM}), frozenset({AGENTS, TASKS, actions_of("spawn")}))
    assert declared("settle") == (frozenset({AGENTS, TASKS, SWARM}), frozenset({SWARM, actions_of("settle")}))


@pytest.mark.feature("swarm-tick-declared-edges")
@pytest.mark.parametrize(
    ("scenario", "reopened"),
    [("lost", "lost engineer@a1b2c3-0099, task t1 reopened"), ("orphan", "task t1 had no agent, reopened")],
)
def test_a_reaped_or_orphaned_task_is_reopened_then_spawned_in_one_pass(
    scenario: str, reopened: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    actions = replay(scenario, "tick", monkeypatch, tmp_path)["actions"][0]
    assert actions.index(reopened) < actions.index("spawned engineer@a1b2c3-0001 for t1")
    assert actions[0] == reopened


@pytest.mark.feature("swarm-tick-declared-edges")
def test_a_drained_swarm_with_new_work_runs_again_before_the_spawn_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = replay("drained", "tick", monkeypatch, tmp_path)
    actions = record["actions"][0]
    assert actions[0] == "new tasks, running again"
    assert actions[-2:] == ["spawned engineer@a1b2c3-0001 for t1", "spawned ci@a1b2c3-0001 for t2"]
    assert "drained" not in actions


@pytest.mark.feature("swarm-tick-declared-edges")
def test_settle_runs_last_and_drains_only_a_pass_with_nothing_left_to_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocked = replay("dependency", "tick", monkeypatch, tmp_path)["actions"][0]
    assert blocked[-1] == "drained"
    assert blocked.count("drained") == 1
    running = replay("running", "tick", monkeypatch, tmp_path)["actions"][0]
    assert running[-1] == "spawned ci@a1b2c3-0001 for t2"
    assert "drained" not in running
