import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from scripts.swarm_ledger import ledger_kinds
from scripts.swarm_ledger.repository import hierarchy

CLOSED = ("done",)
HELD = ("open", "blocked")
Row = dict[str, Any]


def _lane(task: Row) -> object:
    return task.get("lane")


def _kind(task: Row) -> object:
    return ledger_kinds.kind(task)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


SELECTORS: dict[str, Callable[[Row], object]] = {"lane": _lane, "kind": _kind}
SCOPE_PATHS = ("freezes", "phases", "slices", "plans")
FROZEN_IDS = "frozen_ids"
TASK_LINKS = ("phase", "slice", "merged_into")


@dataclass(frozen=True)
class Scope:
    closed: bool = False
    frozen: bool = False


@dataclass(frozen=True)
class Answer:
    rows: list[dict[str, Any]]
    frozen_ids: list[str]
    sources: list[int]


def ancestry(task: dict[str, Any], graph: dict[str, Any]) -> list[str]:
    chain = [f"tasks/{task['id']}"]
    roots: list[str | None] = [hierarchy.parent("tasks", task)]  # pyright: ignore[reportUnknownMemberType]
    if task.get("merged_into"):
        roots.append(f"tasks/{task['merged_into']}")
    for up in roots:
        for _ in range(hierarchy.CHAIN - 1):
            if not up or up in chain:
                break
            chain.append(up)
            up = (graph.get(up) or (None, None))[1]
    return chain


def covers(target: str, task: dict[str, Any], chain: list[str]) -> bool:
    selector, _, value = target.partition(":")
    if selector in SELECTORS and value:
        return SELECTORS[selector](task) == value
    return target in chain


def reshapes(path: list[Any]) -> bool:
    if path[:1] == ["tasks"]:
        return len(path) >= 3 and path[2] in TASK_LINKS
    if not path or path[0] not in SCOPE_PATHS:
        return False
    return path[0] != "phases" or len(path) < 3 or path[2] == "plan"


def targets(records: list[dict[str, Any]], verb: str) -> tuple[set[str], list[str]]:
    named = [record["target"] for record in records if record.get("verb") == verb]
    selectors = [target for target in named if target.partition(":")[0] in SELECTORS and target.partition(":")[2]]
    return set(named) - set(selectors), selectors


def nearest(targets: tuple[set[str], list[str]], task: dict[str, Any], chain: list[str]) -> int:
    nodes, selectors = targets
    level = next((index for index, node in enumerate(chain) if node in nodes), sys.maxsize)
    pairs = (target.partition(":") for target in selectors)
    if level == sys.maxsize and any(SELECTORS[selector](task) == value for selector, _, value in pairs):
        return len(chain)
    return level


def marks(doc: dict[str, Any]) -> dict[str, tuple[bool, int | None]]:
    records: list[dict[str, Any]] = doc.get("freezes") or []
    freezes, focuses = targets(records, "freeze"), targets(records, "focus")
    graph: dict[str, Any] = hierarchy.project(doc)[0] if records else {}  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    found: dict[str, tuple[bool, int | None]] = {}
    tasks: list[Row] = doc.get("tasks") or []
    for task in tasks:
        if not records:
            found[task["id"]] = (False, None)
            continue
        chain = ancestry(task, graph)
        cold, focus = nearest(freezes, task, chain), nearest(focuses, task, chain)
        found[task["id"]] = (cold < focus, None if focus == sys.maxsize else focus)
    return found


def answer(doc: dict[str, Any], scope: Scope) -> Answer:
    resolved = marks(doc)
    kept: list[dict[str, Any]] = []
    held: list[str] = []
    sources: list[int] = []
    tasks: list[Row] = doc.get("tasks") or []
    for index, task in enumerate(tasks):
        cold, focus = resolved[task["id"]]
        if not scope.closed and task.get("state") in CLOSED:
            continue
        if cold and task.get("state") in HELD and not scope.frozen:
            held.append(task["id"])
            continue
        kept.append({**task, "frozen": cold, "focus_rank": focus})
        sources.append(index)
    return Answer(kept, held, sources)
