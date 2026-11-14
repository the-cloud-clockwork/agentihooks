"""The place step's room waits and admission settling act through the tick seams with the launch they hold."""

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import tick
from scripts.tick.steps import place

pytestmark = pytest.mark.unit


@pytest.mark.feature("swarm-tick-launch-cluster")
@pytest.mark.parametrize(
    ("has_gate", "held", "activated"), [(True, "r1", True), (True, "", False), (False, "r1", False)]
)
def test_a_settled_launch_activates_its_held_reservation(
    monkeypatch: pytest.MonkeyPatch, has_gate: bool, held: str, activated: bool
) -> None:
    calls: list[tuple[Any, ...]] = []
    gate = SimpleNamespace(
        held=lambda slug, task: calls.append(("held", slug, task)) or held,
        activate=lambda *args: calls.append(("activate", args)),
    )
    monkeypatch.setattr(tick, "_admission", lambda runtime: gate if has_gate and runtime == "runtime" else None)
    place._settle_admission("sw", "runtime", SimpleNamespace(task="t1"))
    expected: list[tuple[Any, ...]] = [("held", "sw", "t1")] if has_gate else []
    assert calls == expected + ([("activate", ("sw", "t1", "r1"))] if activated else [])


