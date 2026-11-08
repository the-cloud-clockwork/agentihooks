"""The workflows steps of the tick."""

from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep, pass_domains


def _workflow_runner(tick: Any, get: Callable[[str], Any]) -> list[str]:
    actions: list[str] = []
    for task in get("doc").get("tasks", []):
        if (
            task.get("kind") != "workflow"
            or task.get("state") != "claimed"
            or task.get("done")
            or not task.get("claimed_by")
        ):
            continue
        request = {"job": "workflow", "task": task, "repo": get("config").repo}
        if get("tools").host.run(request, deadline=get("deadline")):
            actions.append(f"workflow {task['id']} started")
    return actions


registry.register(
    CallStep(
        "workflow runner",
        "wrapper phases",
        _workflow_runner,
        needs=frozenset({"planning and phase pass", "ledger state", "drained resume"}),
        reads=pass_domains("doc", "config"),
        writes=frozenset(),
        lane=Lane("slow"),
        effects=frozenset({"host"}),
        feature="swarm-workflow-runner",
    )
)
