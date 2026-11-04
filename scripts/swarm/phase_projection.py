"""Each phase's tasks and the states the ledger hierarchy reports, projected from a ledger document with no read."""

from typing import Any

from scripts.swarm_ledger.repository import hierarchy

Nodes = dict[str, tuple[str, str | None, int]]


def _task_state(task: dict[str, Any]) -> str:
    """The state the ledger hierarchy reports for ``task``."""
    if task.get("out_of_scope"):
        return "out_of_scope"
    return task.get("state") or ("done" if task.get("done") else "open")


def _phase_of(node: str | None, nodes: Nodes) -> str | None:
    """The phase node above ``node`` in the projected hierarchy, or nothing when no phase holds it within as many
    steps as the hierarchy has nodes."""
    for _ in nodes:
        if node not in nodes:
            return None
        if nodes[node][0] == "phase":
            return node
        node = nodes[node][1]
    return None


def projected_phase_tasks(doc: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """Each phase's tasks with the states the ledger hierarchy reports, projected from ``doc`` with no ledger read."""
    projected: tuple[Nodes, set[tuple[str, str]]] = hierarchy.project(doc)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    nodes = projected[0]
    tasks: list[dict[str, Any]] = doc.get("tasks") or []
    found: dict[str, list[tuple[str, str]]] = {
        node.removeprefix("phases/"): [] for node, row in nodes.items() if row[0] == "phase"
    }
    for task in tasks:
        node = f"tasks/{task.get('id')}"
        if node in nodes and (phase := _phase_of(nodes[node][1], nodes)) is not None:
            found[phase.removeprefix("phases/")].append((task["id"], _task_state(task)))
    return found
