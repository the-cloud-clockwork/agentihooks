"""Whether a freeze or a focus on the ledger holds an open task back from a claim, and the drain notice naming them."""

from collections.abc import Iterable

from scripts.doctor import loop, priming
from scripts.swarm import notice_text
from scripts.swarm.store import SwarmConfig
from scripts.swarm_ledger import ledger_kinds
from scripts.swarm_ledger.repository import hierarchy

DRAINED = "The swarm has no task left to start"
SELECTORS = {"lane": lambda task: task.get("lane"), "kind": ledger_kinds.kind}


def held(task: dict, doc: dict, graph: dict, fix_phase: str) -> bool:
    if task.get("state") != "open":
        return False
    records = doc.get("freezes") or []
    chain = ancestry(task, graph)
    if any(r["verb"] == "freeze" and covers(r["target"], task, chain) for r in records):
        return True
    focus = [r["target"] for r in records if r["verb"] == "focus"]
    if not focus or exempt(task, fix_phase):
        return False
    return not any(covers(target, task, chain) for target in focus)


def exempt(task: dict, fix_phase: str) -> bool:
    return task.get("rank") == "urgent" or bool(fix_phase) and task.get("phase") == fix_phase


def holds(record: dict, task: dict, graph: dict, fix_phase: str) -> bool:
    inside = covers(record["target"], task, ancestry(task, graph))
    if record["verb"] == "freeze":
        return inside
    return not inside and not exempt(task, fix_phase)


def ancestry(task: dict, graph: dict) -> list:
    chain = [f"tasks/{task['id']}"]
    up = hierarchy.parent("tasks", task)
    while up and up not in chain and len(chain) < hierarchy.CHAIN:
        chain.append(up)
        up = (graph.get(up) or (None, None))[1]
    return chain


def covers(target: str, task: dict, chain: list) -> bool:
    selector, _, value = target.partition(":")
    if selector in SELECTORS and value:
        return SELECTORS[selector](task) == value
    return target in chain


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
    holding = [name for r, name in zip(records, names(doc)) if any(holds(r, t, graph, phase) for t in waiting)]
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
