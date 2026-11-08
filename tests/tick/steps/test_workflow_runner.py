"""The workflow runner tick step registers with its group, lane, feature and ports."""

from importlib import import_module

import pytest

from scripts.tick import registry


@pytest.mark.feature("swarm-workflow-runner")
def test_workflow_runner() -> None:
    """The workflow runner step registers in group wrapper phases on the slow lane with its feature and ports."""
    import_module("scripts.tick.steps.workflows")
    found = {step.name: step for step in registry.steps()}["workflow runner"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "wrapper phases",
        "slow",
        "swarm-workflow-runner",
        frozenset({"host", "inbox", "llm"}),
    )


@pytest.mark.feature("swarm-workflow-runner")
def test_tick_passes_claimed_snapshot_to_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import Mock

    from scripts.tick.steps import CallStep, pass_domains, workflows

    advance = Mock(return_value=["workflow advanced"])
    monkeypatch.setattr(workflows.workflow_runner, "advance_tasks", advance)
    inbox = object()
    doc = {"tasks": [{"kind": "workflow", "state": "claimed"}]}
    config = Mock(return_value=SimpleNamespace(repo="/proof/repo"))
    values = {"inbox": inbox, "slug": "proof", "doc": doc, "store": SimpleNamespace(config=config)}
    found = {step.name: step for step in registry.steps()}["workflow runner"]
    assert isinstance(found, CallStep)
    assert found.call(None, values.__getitem__) == ["workflow advanced"]
    advance.assert_called_once_with(inbox, "proof", doc, Path("/proof/repo"))
    config.assert_called_once_with("proof")
    assert found.needs == frozenset({"planning and phase pass", "ledger state"})
    assert found.reads == pass_domains("inbox", "slug", "doc", "store")
