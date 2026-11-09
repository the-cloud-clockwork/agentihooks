import sys
from dataclasses import dataclass, field
from typing import Any

from scripts.swarm_ledger import ledger_kinds
from scripts.swarm_ledger.repository import hierarchy

CLOSED = ("done",)
SELECTORS = {"lane": lambda task: task.get("lane"), "kind": ledger_kinds.kind}
SCOPE_PATHS = ("freezes", "phases", "slices", "plans")
FROZEN_IDS = "frozen_ids"


@dataclass(frozen=True)
class Scope:
    closed: bool = False
    frozen: bool = False


@dataclass(frozen=True)
class Answer:
    rows: list[dict[str, Any]]
    frozen_ids: list[str] = field(default_factory=list)


def ancestry(task: dict[str, Any], graph: dict[str, Any]) -> list[str]:
    chain = [f"tasks/{task['id']}"]
    roots = [hierarchy.parent("tasks", task)]
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
    if not path or path[0] not in SCOPE_PATHS:
        return False
    return path[0] != "phases" or len(path) < 3 or path[2] == "plan"


def _targets(records: list[dict[str, Any]], verb: str) -> tuple[set[str], list[str]]:
    named = [record["target"] for record in records if record.get("verb") == verb]
    selectors = [target for target in named if target.partition(":")[0] in SELECTORS and target.partition(":")[2]]
    return set(named) - set(selectors), selectors


def _nearest(targets: tuple[set[str], list[str]], task: dict[str, Any], chain: list[str]) -> int:
    nodes, selectors = targets
    level = next((index for index, node in enumerate(chain) if node in nodes), sys.maxsize)
    if level == sys.maxsize and any(covers(target, task, chain) for target in selectors):
        return len(chain)
    return level


def marks(doc: dict[str, Any]) -> dict[str, tuple[bool, int | None]]:
    records: list[dict[str, Any]] = doc.get("freezes") or []
    freezes, focuses = _targets(records, "freeze"), _targets(records, "focus")
    graph = hierarchy.project(doc)[0] if records else {}
    found: dict[str, tuple[bool, int | None]] = {}
    for task in doc.get("tasks") or []:
        if not records:
            found[task["id"]] = (False, None)
            continue
        chain = ancestry(task, graph)
        cold, focus = _nearest(freezes, task, chain), _nearest(focuses, task, chain)
        found[task["id"]] = (task.get("state") == "open" and cold < focus, None if focus == sys.maxsize else focus)
    return found


def answer(doc: dict[str, Any], scope: Scope) -> Answer:
    resolved = marks(doc)
    kept: list[dict[str, Any]] = []
    held: list[str] = []
    for task in doc.get("tasks") or []:
        cold, focus = resolved[task["id"]]
        if not scope.closed and task.get("state") in CLOSED:
            continue
        if cold and not scope.frozen:
            held.append(task["id"])
            continue
        kept.append({**task, "frozen": cold, "focus_rank": focus})
    return Answer(kept, held)


def rows(doc: dict[str, Any], scope: Scope) -> list[dict[str, Any]]:
    return answer(doc, scope).rows
