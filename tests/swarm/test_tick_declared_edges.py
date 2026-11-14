"""Declared step needs, reads and writes keep today's tick constraints with no hand written order."""

from pathlib import Path

import pytest

import scripts.swarm.cli  # noqa: F401
from scripts.tick import engine, registry
from scripts.tick.steps import AGENTS, SWARM, TASKS, actions_of, pass_domains
from tests.swarm.tick_scenarios.test_characterisation import replay

TODAY = {
    "start": ["config load", "stopped branch"],
    "pass setup": ["spawn hold clear", "pass budget"],
    "observe": [
        "pane evidence",
        "execution reconcile",
        "master move fail over",
        "recover master",
        "ledger state",
        "automated cleanup",
        "exits sweep",
        "master start watch",
        "launch checks",
        "track outages",
        "verify",
        "lab capacity",
    ],
    "agents": ["reap", "recover outages", "strays", "failed panes"],
    "drained resume": ["sleeping", "drained resume"],
    "tasks": ["orphans"],
    "reopen passes": [
        "dev red reopen",
        "prerequisite reopen",
        "promotion refresh",
        "release alert",
        "fleet red notice",
    ],
    "placement notices": ["quota notice", "lost runner", "time left"],
    "task passes": ["run window", "verification notices", "size pass", "grouping release", "dispatcher group"],
    "placement acks": ["codex hook order"],
    "placement": ["rank pass", "bottleneck remedy"],
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
        "doc items map",
        "progress heads",
    ],
    "wrapper metrics": ["metrics record", "bottleneck status", "inbox wake", "snapshot job", "knowledge warm"],
    "closing": ["last tick", "heartbeat", "status publish", "step starvation"],
}
EDGES = (
    ("reap", "orphans"),
    ("stall watch", "overrun pass"),
    ("health findings", "findings pass"),
    ("health findings", "metrics record"),
    ("sleeping", "drained resume"),
    ("dev red reopen", "prerequisite reopen"),
)


def wave_of() -> dict[str, int]:
    return {step.name: index for index, wave in enumerate(registry.waves()) for step in wave}


def declared(name: str) -> tuple[frozenset[str], frozenset[str]]:
    step = next(step for step in registry.steps() if step.name == name)
    return step.reads, step.writes


@pytest.mark.feature("swarm-tick-declared-edges")
def test_registry_waves_reproduce_todays_constraints() -> None:
    waves = wave_of()
    for before, after in EDGES:
        assert waves[before] < waves[after], (before, after)
    critical = {step.name: waves[step.name] for step in registry.steps() if step.lane != engine.SLOW}
    assert waves["settle"] == max(waves.values())
    assert [name for name, wave in critical.items() if wave == max(waves.values())] == ["settle"]


@pytest.mark.feature("swarm-tick-declared-edges")
def test_every_group_runs_in_todays_order_from_needs_and_registration() -> None:
    wave_of()
    for group, names in TODAY.items():
        assert [step.name for step in engine.group_steps(group)] == names, group
    assert all(not hasattr(step, "order") for step in registry.steps())


@pytest.mark.feature("swarm-tick-declared-edges")
def test_the_steps_on_each_edge_declare_what_they_read_and_write() -> None:
    wave_of()
    state = pass_domains("config", "rows", "doc")
    assert declared("reap") == (
        frozenset({AGENTS}) | pass_domains("rows"),
        frozenset({AGENTS, TASKS, actions_of("reap")}),
    )
    assert declared("orphans") == (
        frozenset({TASKS}) | pass_domains("config", "rows"),
        frozenset({TASKS, actions_of("orphans")}),
    )
    assert declared("settle") == (
        frozenset({AGENTS, TASKS, SWARM}) | state,
        frozenset({AGENTS, TASKS, SWARM, actions_of("settle")}),
    )


@pytest.mark.feature("swarm-tick-declared-edges")
def test_settle_runs_last_and_drains_only_a_pass_with_nothing_left_to_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocked = replay("dependency", "tick", monkeypatch, tmp_path)["actions"][0]
    assert blocked[-1] == "drained"
    assert blocked.count("drained") == 1
