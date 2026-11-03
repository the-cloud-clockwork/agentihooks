from collections.abc import Callable

from scripts.swarm import freeze, profile_first
from scripts.swarm_ledger.repository import hierarchy


def ranks(doc: dict) -> dict[str, int]:
    """Rank each task by the first focus record covering it, the record count when none does."""
    targets = [row["target"] for row in doc.get("freezes", []) if row["verb"] == "focus"]
    graph = hierarchy.project(doc)[0]
    paths: dict[str, int] = {}
    selected = []
    for index, target in enumerate(targets):
        if _selects(target):
            selected.append((index, target))
        else:
            paths.setdefault(target, index)
    return {
        task["id"]: _rank(task, freeze.ancestry(task, graph), paths, selected, len(targets)) for task in doc["tasks"]
    }


def _selects(target: str) -> bool:
    selector, _, value = target.partition(":")
    return selector in freeze.SELECTORS and bool(value)


def _rank(task: dict, chain: list, paths: dict[str, int], selected: list, unfocused: int) -> int:
    found = [paths[node] for node in chain if node in paths]
    found += [next((index for index, target in selected if freeze.covers(target, task, chain)), unfocused)]
    return min(found)


def select(doc: dict, ready: list, lane: str, capacity: int, running: list | None = None) -> list:
    if running is None:
        running = [task for task in doc["tasks"] if task.get("state") in ("claimed", "pr")]
    selected, _ = place(doc, ready, lane, capacity, running, lambda tasks, room: (tasks[:room], []))
    return selected


def place(doc: dict, ready: list, lane: str, capacity: int, running: list, locate: Callable) -> tuple[list, list]:
    """Focused tasks take the lane's free seats first in focus queue order; free tasks fill what is left."""
    order = ranks(doc)
    unfocused = sum(row["verb"] == "focus" for row in doc.get("freezes", []))
    room = max(0, capacity - sum(task.get("lane") == lane for task in running))
    normal, notes = locate([task for task in ready if order[task["id"]] == unfocused], room)
    plain = [task for task in normal if profile_first.UNRESOLVED not in task]
    hot = sorted((task for task in ready if order[task["id"]] < unfocused), key=lambda task: order[task["id"]])
    focused, refused = locate(hot, room)
    placed = [task for task in focused if profile_first.UNRESOLVED not in task]
    pending = [task for task in normal + focused if profile_first.UNRESOLVED in task]
    return pending + placed + plain[: room - len(placed)], notes + refused
