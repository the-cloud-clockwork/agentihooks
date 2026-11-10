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
        frozenset({"host"}),
    )


@pytest.mark.feature("swarm-workflow-runner")
def test_tick_passes_claimed_snapshot_to_runner() -> None:
    from types import SimpleNamespace

    from scripts.tick.adapters.fakes import FakeHost, fake_tools
    from scripts.tick.deadline import Deadline
    from scripts.tick.steps import CallStep, pass_domains

    task = {"id": "work", "kind": "workflow", "state": "claimed", "claimed_by": "engineer", "done": False}
    tools = fake_tools()
    assert isinstance(tools.host, FakeHost)
    tools.host.answers["run"] = True
    deadline = Deadline.after(5)
    values = {
        "doc": {
            "tasks": [
                {**task, "kind": "code"},
                {**task, "done": True},
                {**task, "state": "open"},
                {**task, "claimed_by": ""},
                task,
            ]
        },
        "config": SimpleNamespace(repo="/proof/repo"),
        "tools": tools.for_effects(frozenset({"host"})),
        "deadline": deadline,
    }
    found = {step.name: step for step in registry.steps()}["workflow runner"]
    assert isinstance(found, CallStep)
    assert found.call(None, values.__getitem__) == ["workflow work started"]
    assert len(tools.host.calls) == 1
    assert tools.host.calls[0].args == ({"job": "workflow", "task": task, "repo": "/proof/repo"},)
    assert tools.host.calls[0].deadline is deadline
    assert found.needs == frozenset({"planning and phase pass", "ledger state", "drained resume"})
    assert found.reads == pass_domains("doc", "config")
    values["deadline"] = Deadline.after(-1)
    with pytest.raises(TimeoutError, match="tool deadline expired"):
        found.call(None, values.__getitem__)
    assert len(tools.host.calls) == 1


@pytest.mark.feature("swarm-workflow-runner")
def test_tick_without_task_collection_starts_no_jobs() -> None:
    from scripts.tick.steps import CallStep

    found = {step.name: step for step in registry.steps()}["workflow runner"]
    assert isinstance(found, CallStep)
    assert found.call(None, {"doc": {}}.__getitem__) == []


@pytest.mark.feature("swarm-workflow-runner")
def test_wrapper_phases_pass_box_without_config_still_starts_the_claimed_workflow() -> None:
    """The wrapper phases site boxes only the ledger doc, so the step reads the swarm repo from the store."""
    from types import SimpleNamespace

    from scripts.tick.adapters.fakes import FakeHost, fake_tools
    from scripts.tick.deadline import Deadline
    from scripts.tick.sites import _GraphView  # pyright: ignore[reportPrivateUsage]
    from scripts.tick.step import Context

    task = {"id": "work", "kind": "workflow", "state": "claimed", "claimed_by": "engineer", "done": False}
    tools = fake_tools()
    assert isinstance(tools.host, FakeHost)
    tools.host.answers["run"] = True
    configs = {"proof": SimpleNamespace(repo="/proof/repo")}
    state = {"store": SimpleNamespace(config=configs.__getitem__), "slug": "proof"}
    view = _GraphView(state, {"doc": {"tasks": [task]}})
    found = {step.name: step for step in registry.steps()}["workflow runner"]
    result = found.run(Context(view, Deadline.after(5), tools.for_effects(frozenset({"host"}))))
    assert [write.value for write in result.writes] == [["workflow work started"]]
    assert tools.host.calls[0].args == ({"job": "workflow", "task": task, "repo": "/proof/repo"},)
