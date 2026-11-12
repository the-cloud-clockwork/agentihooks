"""The progress heads tick step registers with its group, lane, feature and ports."""

from importlib import import_module

import pytest

from scripts.tick import registry


@pytest.mark.feature("tick-progress-heads")
def test_progress_heads() -> None:
    """The progress heads step registers in group wrapper events on the slow lane with its feature and ports."""
    import_module("scripts.tick.steps.report")
    found = {step.name: step for step in registry.steps()}["progress heads"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "wrapper events",
        "slow",
        "tick-progress-heads",
        frozenset({"source_control"}),
    )


@pytest.mark.feature("tick-progress-heads")
def test_progress_heads_reads_held_tasks_through_the_pass_view_and_the_swarm_origin(monkeypatch) -> None:
    """The body hands the swarm's tasks, the pass view, the tick clock and the swarm repository's origin to heads_pass."""
    from types import SimpleNamespace

    from scripts.gates import progress
    from scripts.tick.steps import report

    seen = []
    redis, view, tasks = object(), object(), [{"id": "t1"}]
    state = {
        "store": SimpleNamespace(redis=redis),
        "slug": "sw",
        "doc": {"tasks": tasks},
        "view": view,
        "config": SimpleNamespace(repo="/repo"),
    }
    tick = SimpleNamespace(now_ms=lambda: 42, skip_refused=lambda step, *args: step(*args))
    monkeypatch.setattr(progress, "origin", lambda path: f"https://github.com/o{path}")
    monkeypatch.setattr(progress, "heads_pass", lambda *args: seen.append(args) or ["pushed"])
    assert report._progress_heads(tick, state.__getitem__) == ["pushed"]
    assert seen == [(redis, "sw", tasks, view, 42, "https://github.com/o/repo")]
