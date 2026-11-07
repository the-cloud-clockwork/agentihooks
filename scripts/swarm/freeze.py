"""Whether a freeze or a focus on the ledger holds an open task back from a claim, and the drain notice naming them."""

import operator
import sys
from collections.abc import Callable, Iterable
from typing import Any

from scripts.doctor import loop, priming
from scripts.swarm import notice_text
from scripts.swarm.ledger_client import pass_state
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from scripts.swarm_ledger import ledger_kinds
from scripts.swarm_ledger.repository import hierarchy

DRAINED = "The swarm has no task left to start"
SELECTORS = {"lane": lambda task: task.get("lane"), "kind": ledger_kinds.kind}
WATCHED = "watched_focus"


def held(task: dict, doc: dict, graph: dict, fix_phase: str) -> bool:
    """A freeze, a focus with open work elsewhere or a watched focus keeps a claim off the open task."""
    if task.get("state") != "open":
        return False
    records, chain = doc.get("freezes") or [], ancestry(task, graph)
    return (
        frozen(task, records, chain) or unfocused(task, doc, graph, fix_phase) or outside_watched(task, doc, fix_phase)
    )


claim_gate = held


def outside_watched(task: dict, doc: dict, fix_phase: str) -> bool:
    return bool(doc.get(WATCHED)) and not exempt(task, fix_phase)


def watched(slug: str, store: RedisStore, ledger: Any, doc: dict[str, Any]) -> dict[str, Any]:
    peer = store.peer(slug) if fix_phase(store.config(slug)) else ""
    if not peer:
        return doc
    try:
        peer_doc = pass_state(ledger, peer)
    except SwarmError:
        return doc
    freezes: list[dict[str, Any]] = peer_doc.get("freezes") or []
    focus = [r["target"] for r in freezes if r["verb"] == "focus"]
    return {**doc, WATCHED: [f"the focus on {_named(peer_doc, target)} in the watched swarm" for target in focus]}


def frozen(task: dict[str, Any], records: list[dict[str, Any]], chain: list[str]) -> bool:
    return _nearest(records, "freeze", task, chain) < _nearest(records, "focus", task, chain)


def _nearest(records: list[dict[str, Any]], verb: str, task: dict[str, Any], chain: list[str]) -> int:
    targets: list[str] = [r["target"] for r in records if r["verb"] == verb]
    levels = [chain.index(t) if t in chain else len(chain) for t in targets if covers(t, task, chain)]
    return min(levels, default=sys.maxsize)


def unfocused(task: dict[str, Any], doc: dict[str, Any], graph: dict[str, Any], fix_phase: str) -> bool:
    queue = admitted(doc, graph)
    return queue is not None and task["id"] not in queue and not _fix(task, fix_phase)


_ADMITTED: list[Any] = []


def admitted(doc: dict[str, Any], graph: dict[str, Any]) -> frozenset[str] | None:
    """The open focused tasks and every task they wait on, once per ledger view; None while no focus has open work."""
    tasks: list[dict[str, Any]] = doc.get("tasks") or []
    key = (doc, tasks, doc.get("freezes"), graph)
    states = [task.get("state") for task in tasks]
    if _ADMITTED and _ADMITTED[1] == states and all(map(operator.is_, _ADMITTED[0], key)):
        return _ADMITTED[2]
    found = _admit(doc, graph)
    _ADMITTED[:] = [key, states, found]
    return found


def _admit(doc: dict[str, Any], graph: dict[str, Any]) -> frozenset[str] | None:
    records: list[dict[str, Any]] = doc.get("freezes") or []
    tasks: list[dict[str, Any]] = doc.get("tasks") or []
    focus: list[str] = [r["target"] for r in records if r["verb"] == "focus"]
    if not focus:
        return None
    rows = {task["id"]: task for task in tasks}
    waits_on = _waits(doc, tasks)
    cold: set[str] = _cold(tasks, records, graph) if any(r["verb"] == "freeze" for r in records) else set()
    queue = [
        task["id"] for task in tasks if _open_focus(task, focus, graph) and not _stuck(task["id"], rows, cold, waits_on)
    ]
    seen: set[str] = set()
    for task_id in queue:
        _reach(task_id, rows, cold, waits_on, seen)
    return frozenset(seen) if seen else None


def _reach(
    task_id: str,
    rows: dict[str, dict[str, Any]],
    cold: set[str],
    waits_on: Callable[[dict[str, Any]], list[str]],
    seen: set[str],
) -> None:
    """Add the task and everything it waits on, short of frozen work, to the admitted set."""
    if task_id in seen or task_id in cold:
        return
    seen.add(task_id)
    for dep in waits_on(rows.get(task_id) or {}):
        _reach(dep, rows, cold, waits_on, seen)


def _cold(tasks: list[dict[str, Any]], records: list[dict[str, Any]], graph: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for task in tasks:
        if task.get("state") == "open" and frozen(task, records, ancestry(task, graph)):
            found.add(task["id"])
    return found


def _waits(doc: dict[str, Any], tasks: list[dict[str, Any]]) -> Callable[[dict[str, Any]], list[str]]:
    """What a task waits on: its dependencies, the tasks it is parked on and every task of each phase its phase
    waits on."""
    phases: list[dict[str, Any]] = doc.get("phases") or []
    after: dict[str | None, list[str]] = {p["id"]: p.get("depends_on") or [] for p in phases}
    members: dict[str | None, list[str]] = {}
    for task in tasks:
        members.setdefault(task.get("phase"), []).append(task["id"])

    def waits_on(row: dict[str, Any]) -> list[str]:
        named: list[str] = [*(row.get("depends_on") or []), *(row.get("parked_on") or [])]
        return named + [member for phase in after.get(row.get("phase"), []) for member in members.get(phase, [])]

    return waits_on


def _stuck(
    task_id: str,
    rows: dict[str, dict[str, Any]],
    cold: set[str],
    waits_on: Callable[[dict[str, Any]], list[str]],
) -> bool:
    """Whether the task waits, directly or through other open work, on a frozen task, so it can never run."""
    reached: set[str] = set()
    _reach(task_id, rows, set(), lambda row: [] if row.get("state") == "done" else waits_on(row), reached)
    return bool(reached & cold)


def _open_focus(task: dict[str, Any], focus: list[str], graph: dict[str, Any]) -> bool:
    if task.get("state") == "done" or task.get("out_of_scope"):
        return False
    chain = ancestry(task, graph)
    return any(covers(target, task, chain) for target in focus)


def exempt(task: dict[str, Any], fix_phase: str) -> bool:
    return task.get("rank") == "urgent" or _fix(task, fix_phase)


def _fix(task: dict[str, Any], fix_phase: str) -> bool:
    return bool(fix_phase) and task.get("phase") == fix_phase


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


def covers(target: str, task: dict, chain: list) -> bool:
    selector, _, value = target.partition(":")
    if selector in SELECTORS and value:
        return SELECTORS[selector](task) == value
    return target in chain


def marks(doc: dict, graph: dict, fix: str, watched: bool) -> dict:
    records = doc.get("freezes") or []
    focus = [r["target"] for r in records if r["verb"] == "focus"]
    tasks = {f"tasks/{t.get('id')}": t for t in doc.get("tasks") or [] if isinstance(t, dict)}
    chains = {node: _chain(node, graph, tasks) for node in graph}
    found, focused = {"frozen": [], "held": []}, admitted(doc, graph) is not None
    for node, chain in chains.items():
        task = tasks.get(node)
        if task is not None:
            cold = frozen(task, records, chain)
            hold = task.get("state") == "open" and (
                unfocused(task, doc, graph, fix) or watched and not exempt(task, fix)
            )
        else:
            cold = any(r["verb"] == "freeze" and r["target"] in chain for r in records)
            hold = focused and not any(_reaches(target, node, chains) for target in focus)
        if cold or hold:
            found["frozen" if cold else "held"].append(node)
    return found


def _chain(node: str, graph: dict, tasks: dict, below: tuple = ()) -> list:
    if node in tasks:
        return ancestry(tasks[node], graph)
    chain = [*below, node]
    up = (graph.get(node) or (None, None))[1]
    return _chain(up, graph, tasks, tuple(chain)) if up not in (None, *chain) else chain


def _reaches(target: str, node: str, chains: dict) -> bool:
    return target.partition(":")[0] in SELECTORS or target in chains[node] or node in chains.get(target, ())


def fix_phase(config: SwarmConfig) -> str:
    return loop.FIX_PHASE if config.template == priming.TEMPLATE else ""


def names(doc: dict) -> list:
    return [f"the {r['verb']} on {_named(doc, r['target'])}" for r in doc.get("freezes") or []]


def notice(doc: dict, tasks: Iterable[dict], phase: str) -> str:
    graph = hierarchy.project(doc)[0]
    waiting = [t for t in tasks if not t.get("out_of_scope") and held(t, doc, graph, phase)]
    if not waiting:
        return DRAINED
    records = doc.get("freezes") or []
    chains = [(t, ancestry(t, graph)) for t in waiting]
    focused_out = any(unfocused(t, doc, graph, phase) for t, _ in chains)
    holding = [
        name
        for r, name in zip(records, names(doc))
        if (focused_out if r["verb"] == "focus" else any(covers(r["target"], t, chain) for t, chain in chains))
    ]
    if any(outside_watched(t, doc, phase) for t in waiting):
        holding += doc[WATCHED]
    which = "1 open task is" if len(waiting) == 1 else f"{len(waiting)} open tasks are"
    return notice_text.plain(f"The swarm has no task it may start: {which} held by {_joined(holding)}")


def _named(doc: dict, target: str) -> str:
    selector, _, value = target.partition(":")
    if selector == "lane":
        return f"the {value} lane"
    if selector == "kind":
        return f"{value} tasks"
    collection, _, item_id = target.partition("/")
    item = next((i for i in doc.get(collection) or [] if i.get("id") == item_id), {})
    return f"{hierarchy.KINDS.get(collection, collection)} {item.get('title') or item_id}"


def _joined(parts: list) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
