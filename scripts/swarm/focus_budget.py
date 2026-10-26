from collections.abc import Callable

from scripts.swarm import freeze, profile_first
from scripts.swarm_ledger.repository import hierarchy


def ranks(doc: dict) -> dict[str, int]:
    targets = [row["target"] for row in doc.get("freezes", []) if row["verb"] == "focus"]
    graph = hierarchy.project(doc)[0]
    return {
        task["id"]: next(
            (
                index
                for index, target in enumerate(targets)
                if freeze.covers(target, task, freeze.ancestry(task, graph))
            ),
            len(targets),
        )
        for task in doc["tasks"]
    }


def select(doc: dict, ready: list, lane: str, capacity: int, weight: int, running: list | None = None) -> list:
    if running is None:
        running = [task for task in doc["tasks"] if task.get("state") in ("claimed", "pr")]
    selected, _ = place(doc, ready, lane, capacity, weight, running, lambda tasks, room: (tasks[:room], []))
    return selected


def place(
    doc: dict, ready: list, lane: str, capacity: int, weight: int, running: list, locate: Callable
) -> tuple[list, list]:
    order = ranks(doc)
    unfocused = sum(row["verb"] == "focus" for row in doc.get("freezes", []))
    live = [task for task in running if task.get("lane") == lane]
    holding = sum(order.get(task["id"], unfocused) < unfocused for task in live)
    room = max(0, capacity - len(live))
    normal, notes = locate([task for task in ready if order[task["id"]] == unfocused], room)
    plain = [task for task in normal if profile_first.UNRESOLVED not in task]
    slots = allowance(capacity, weight, len(live), holding, len(plain))
    hot = sorted((task for task in ready if order[task["id"]] < unfocused), key=lambda task: order[task["id"]])
    focused, refused = locate(hot, slots)
    placed = [task for task in focused if profile_first.UNRESOLVED not in task]
    pending = [task for task in normal + focused if profile_first.UNRESOLVED in task]
    return pending + placed + plain[: room - len(placed)], notes + refused


def allowance(capacity: int, weight: int, busy: int, focused: int, normal: int) -> int:
    room = max(0, capacity - busy)
    if weight == 100:
        return room
    share = (weight * (busy + normal) - 100 * focused) // (100 - weight)
    return max(0, min(room, capacity * weight // 100 - focused, share))
