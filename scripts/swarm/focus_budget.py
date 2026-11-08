from collections.abc import Callable
from functools import partial
from typing import Any

from scripts.swarm import freeze, profile_first
from scripts.swarm_ledger import ledger_rank
from scripts.swarm_ledger.repository import hierarchy


def ranks(doc: dict[str, Any]) -> dict[str, int]:
    """Rank each task by the first focus record covering it, the record count when none does."""
    rows: list[dict[str, str]] = doc.get("freezes", [])
    targets = [row["target"] for row in rows if row["verb"] == "focus"]
    graph: dict[str, Any] = hierarchy.project(doc)[0]
    tasks: list[dict[str, Any]] = doc["tasks"]
    selectors: dict[str, Callable[[dict[str, Any]], object]] = freeze.SELECTORS
    paths: dict[str, int] = {}
    selected: list[tuple[int, Callable[[dict[str, Any]], object], str]] = []
    for index, target in enumerate(targets):
        selector, _, value = target.partition(":")
        if selector in selectors and value:
            selected.append((index, selectors[selector], value))
        else:
            paths.setdefault(target, index)
    return {str(task["id"]): _rank(task, freeze.ancestry(task, graph), paths, selected, len(targets)) for task in tasks}


def ordered(
    queue: list[tuple[str, dict[str, Any]]],
    focus: dict[str, int],
    band: Callable[[dict[str, Any]], Any] = lambda task: 0,
    unfocused: int | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """The queue with focused tasks first, then by rank band, then by focus rank, ties kept in queue order."""
    return sorted(queue, key=lambda entry: (focus[entry[1]["id"]] == unfocused, band(entry[1]), focus[entry[1]["id"]]))


def outside(doc: dict[str, Any]) -> int:
    """The focus rank of a task no focus record covers: the count of focus records."""
    return sum(row["verb"] == "focus" for row in doc.get("freezes", []))


def _hot_key(order: dict[str, int], task: dict[str, Any]) -> tuple[int, int]:
    return ledger_rank.order(task), order[task["id"]]


def _rank(
    task: dict[str, Any],
    chain: list[str],
    paths: dict[str, int],
    selected: list[tuple[int, Callable[[dict[str, Any]], object], str]],
    unfocused: int,
) -> int:
    found = [paths[node] for node in chain if node in paths]
    found += [next((index for index, pick, value in selected if pick(task) == value), unfocused)]
    return min(found)


def select(doc: dict, ready: list, lane: str, capacity: int, running: list | None = None) -> list:
    if running is None:
        running = [task for task in doc["tasks"] if task.get("state") in ("claimed", "pr")]
    selected, _ = place(doc, ready, lane, capacity, running, lambda tasks, room: (tasks[:room], []))
    return selected


def place(doc: dict, ready: list, lane: str, capacity: int, running: list, locate: Callable) -> tuple[list, list]:
    """Focused tasks take the lane's free seats first in focus queue order; free tasks fill what is left."""
    order = ranks(doc)
    unfocused = outside(doc)
    room = max(0, capacity - sum(task.get("lane") == lane for task in running))
    normal, notes = locate([task for task in ready if order[task["id"]] == unfocused], room)
    plain = [task for task in normal if profile_first.UNRESOLVED not in task]
    hot = sorted(
        (task for task in ready if order[task["id"]] < unfocused),
        key=partial(_hot_key, order),
    )
    focused, refused = locate(hot, room)
    placed = [task for task in focused if profile_first.UNRESOLVED not in task]
    pending = [task for task in normal + focused if profile_first.UNRESOLVED in task]
    return pending + placed + plain[: room - len(placed)], notes + refused
