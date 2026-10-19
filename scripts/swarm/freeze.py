"""Whether a freeze or a focus on the ledger holds an open task back from a claim, and the drain notice naming them."""

from collections.abc import Iterable

from scripts.doctor import loop, priming
from scripts.swarm import notice_text
from scripts.swarm.store import SwarmConfig, SwarmError
from scripts.swarm_ledger import ledger_kinds
from scripts.swarm_ledger.repository import hierarchy

DRAINED = "The swarm has no task left to start"
SELECTORS = {"lane": lambda task: task.get("lane"), "kind": ledger_kinds.kind}
WATCHED = "watched_focus"


def held(task: dict, doc: dict, graph: dict, fix_phase: str) -> bool:
    if task.get("state") != "open":
        return False
    records, chain = doc.get("freezes") or [], ancestry(task, graph)
    return (
        frozen(task, records, chain)
        or unfocused(task, records, chain, fix_phase)
        or outside_watched(task, doc, fix_phase)
    )


def outside_watched(task: dict, doc: dict, fix_phase: str) -> bool:
    return bool(doc.get(WATCHED)) and not exempt(task, fix_phase)


def watched(slug: str, store, ledger, doc: dict) -> dict:
    peer = store.peer(slug) if fix_phase(store.config(slug)) else ""
    if not peer:
        return doc
    try:
        peer_doc = ledger.state(peer)
    except SwarmError:
        return doc
    focus = [r["target"] for r in peer_doc.get("freezes") or [] if r["verb"] == "focus"]
    return {**doc, WATCHED: [f"the focus on {_named(peer_doc, target)} in the watched swarm" for target in focus]}


def frozen(task: dict, records: list, chain: list) -> bool:
    return any(r["verb"] == "freeze" and covers(r["target"], task, chain) for r in records)


def unfocused(task: dict, records: list, chain: list, fix_phase: str) -> bool:
    focus = [r["target"] for r in records if r["verb"] == "focus"]
    return bool(focus) and not exempt(task, fix_phase) and not any(covers(target, task, chain) for target in focus)


def exempt(task: dict, fix_phase: str) -> bool:
    return task.get("rank") == "urgent" or bool(fix_phase) and task.get("phase") == fix_phase


def ancestry(task: dict, graph: dict) -> list:
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
    found = {"frozen": [], "held": []}
    for node, chain in chains.items():
        task = tasks.get(node)
        if task is not None:
            cold = frozen(task, records, chain)
            hold = task.get("state") == "open" and (
                unfocused(task, records, chain, fix) or watched and not exempt(task, fix)
            )
        else:
            cold = any(r["verb"] == "freeze" and r["target"] in chain for r in records)
            hold = bool(focus) and not any(_reaches(target, node, chains) for target in focus)
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
    focused_out = any(unfocused(t, records, chain, phase) for t, chain in chains)
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
