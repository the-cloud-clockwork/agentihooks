"""Tick scenarios replayed on the real step clock: every step inside its floor, none abandoned."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.swarm import timing
from scripts.tick.steps import budgets
from tests.swarm.tick_scenarios import test_characterisation as characterisation


@pytest.fixture(scope="module")
def warmed(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Replay one pass first so cold imports are not charged to the first gated step."""
    with pytest.MonkeyPatch.context() as patch:
        characterisation.replay("running", "run_tick", patch, tmp_path_factory.mktemp("warm"))


def records() -> list[dict[str, Any]]:
    """Return the ``tick:records`` rows the last replay appended through its patched recorder."""
    appended = cast(Mock, timing.append_record)
    return [call.args[3] for call in appended.call_args_list if call.args[2] == "tick:records"]


def overruns(rows: list[dict[str, Any]]) -> list[str]:
    """Return one line per row whose step was abandoned or whose wall exceeds its declared floor."""
    floors = {name: budget.floor_s for name, budget in budgets.FLOORS.items()}
    if source := os.environ.get("TICK_BUDGET_FLOORS"):
        floors.update(json.loads(Path(source).read_text()))
    found: list[str] = []
    for row in rows:
        floor = floors.get(row["step"], row["budget_s"])
        if row["abandoned"]:
            found.append(f"{row['step']} abandoned after {row['wall_s']:.2f}s")
        elif row["wall_s"] > floor:
            found.append(f"{row['step']} took {row['wall_s']:.2f}s over its {floor}s floor")
    return found


@pytest.mark.feature("swarm-tick-step-budget-gate")
@pytest.mark.parametrize("scenario", characterisation.SCENARIOS)
def test_every_scenario_step_finishes_inside_its_floor(
    scenario: str, warmed: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    characterisation.replay(scenario, "run_tick", monkeypatch, tmp_path)
    rows = records()
    output = Path(os.environ.get("TICK_BUDGET_OUTPUT", str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{scenario}.json").write_text(json.dumps(rows, indent=2) + "\n")
    assert rows, f"{scenario} recorded no tick steps"
    assert not overruns(rows), f"{scenario}: " + "; ".join(overruns(rows))


@pytest.mark.feature("swarm-tick-step-budget-gate")
@pytest.mark.parametrize(
    ("row", "line"),
    [
        (
            {"step": "reap", "wall_s": 22.5, "budget_s": 22.0, "abandoned": False},
            "reap took 22.50s over its 22.0s floor",
        ),
        ({"step": "spawn", "wall_s": 44.0, "budget_s": 44.0, "abandoned": True}, "spawn abandoned after 44.00s"),
        (
            {"step": "unlisted", "wall_s": 3.0, "budget_s": 2.0, "abandoned": False},
            "unlisted took 3.00s over its 2.0s floor",
        ),
    ],
)
def test_an_overrun_or_abandoned_step_is_named(row: dict[str, Any], line: str) -> None:
    assert overruns([row]) == [line]


@pytest.mark.feature("swarm-tick-step-budget-gate")
def test_a_step_inside_its_floor_passes() -> None:
    assert overruns([{"step": "reap", "wall_s": 21.9, "budget_s": 22.0, "abandoned": False}]) == []


@pytest.mark.feature("swarm-tick-step-budget-gate")
def test_a_head_floor_increase_cannot_weaken_the_base_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "floors.json"
    source.write_text(json.dumps({"reap": 22.0}))
    monkeypatch.setenv("TICK_BUDGET_FLOORS", str(source))
    monkeypatch.setattr(budgets, "FLOORS", {"reap": SimpleNamespace(floor_s=44.0)})
    assert overruns([{"step": "reap", "wall_s": 30.0, "budget_s": 44.0, "abandoned": False}]) == [
        "reap took 30.00s over its 22.0s floor"
    ]
