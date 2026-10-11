"""The dispatcher's tick step: rank by critical path and work unblocked, grouping and the priority sweep, by autonomy.

Each tick it orders every open ungrouped task, leaves included, by the longest open dependency chain behind it, then
the open work it unblocks, the bottleneck lane, focus order and ledger position. Of the claimable tasks that unblock
work, at delegate and full it raises the top ones to high and lowers a high or urgent rank once that task leaves the
top. A rank the master or the swarm set is a default it reorders; only an operator rank, set by him or relayed with
his quoted words, stays fixed. Below delegate it proposes each raise to the master. Every action is a gate log row, a
dispatch metrics row naming its rule and, for a rank, a comment on the task.
"""

from dataclasses import dataclass
from functools import partial

from scripts.gates import log as gate_log
from scripts.inbox.store import InboxStore
from scripts.swarm import bottleneck, freeze, grouping, metrics_outbox, priority_sweep
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.ledger_events import Mail
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger_rank
from scripts.swarm_ledger.repository import hierarchy

AUTHOR = "dispatcher"
APPLIES = ("delegate", "full")
TOP = 3
RANK, LOWER = "high", "normal"
LANES = {"engineering": "eng", "ci": "ci"}
LEVERAGE, GROUPING, PRIORITIES = "leverage-rank", "grouping", "priority-sweep"
SKIPPED = "skipped "
WINDOW = (None, "high", "urgent")
UNCOMMENTED = "; the ledger did not take its comment"
TABLE = metrics_outbox.Table("dispatch_actions", (("rule", "String"), ("mode", "String"), ("action", "String")))
RAISED = "The dispatcher raised this task to high rank because it unblocks {work}."
LOWERED = "The dispatcher lowered this task to normal rank because {why}."
OUTRANKED = "it unblocks {work} and {top} claimable tasks in focus lead longer chains or unblock more"
UNREADY = "it is no longer a claimable task in focus that unblocks open work"
IDLE = "it unblocks no open work"
PROPOSED = "The dispatcher proposed high rank for this task to the master because it unblocks {work}."
PROPOSE = (
    "The dispatcher proposes rank high for task {task} titled {title}: it unblocks {work}. If the operator agrees, "
    "apply it with agentihooks ledger --slug {slug} --as <your name> task set {task} rank=high."
)


@dataclass(frozen=True)
class Action:
    rule: str
    mode: str
    text: str
    task: dict


def leverage(doc: dict) -> dict[str, int]:
    waiters = _waiters(doc)
    return {task_id: len(_downstream(task_id, waiters)) for task_id in waiters}


def chains(doc: dict) -> dict[str, int]:
    waiters, found = _waiters(doc), {}
    reach = {task_id: _downstream(task_id, waiters) for task_id in waiters}
    for task_id in sorted(waiters, key=lambda t: len(reach[t])):
        # A dependency loop counts as one block of its size: the longest simple path through it is NP-hard.
        loop = {task_id} | {w for w in reach[task_id] if task_id in reach[w]}
        after = {w for member in loop for w in waiters[member]} - loop
        found.update(dict.fromkeys(loop, len(loop) - 1 + max((found[w] + 1 for w in after), default=0)))
    return {task_id: found[task_id] for task_id in waiters}


def _waiters(doc):
    tasks = {t["id"]: t for t in doc["tasks"] if _open(t)}
    nodes, edges = hierarchy.project(doc)
    under = {}
    for task_id in tasks:
        for node in _lineage(f"tasks/{task_id}", nodes):
            under.setdefault(node, set()).add(task_id)
    required = {}
    for node, needed in edges:
        required.setdefault(node, set()).add(needed)
    waiters = {task_id: set() for task_id in tasks}
    for task_id in tasks:
        for node in _lineage(f"tasks/{task_id}", nodes):
            for needed in required.get(node, ()):
                for blocker in under.get(needed, ()):
                    waiters[blocker].add(task_id)
    return waiters


def ordered(doc: dict, named: str, scores: dict) -> list[str]:
    lane, depth, focus = LANES.get(named), chains(doc), _focus_order(doc)
    position = {t["id"]: index for index, t in enumerate(doc["tasks"])}
    rows = [t for t in doc["tasks"] if t["id"] in depth and t.get("state") == "open" and not t.get("merged_into")]
    return [
        t["id"]
        for t in sorted(
            rows,
            key=lambda t: (
                -depth[t["id"]],
                -scores.get(t["id"], 0),
                t.get("lane") != lane,
                focus[t["id"]],
                position[t["id"]],
            ),
        )
    ]


def rank_pass(slug, config, store, ledger, doc, now_ms):
    from scripts.swarm import tick

    scores, known, owned, fixed = leverage(doc), {t["id"]: t for t in doc["tasks"]}, _owned(doc), _fixed(doc)
    named = bottleneck.read(store, slug).get("bottleneck")
    graph, fix = hierarchy.project(doc)[0], freeze.fix_phase(config)
    claimable = {task_id for task_id, t in known.items() if tick.ready(t, known, doc, graph, fix)}
    queue = [task_id for task_id in ordered(doc, named, scores) if task_id in claimable and scores[task_id]]
    top = [task_id for task_id in queue if task_id not in fixed or known[task_id].get("rank") in WINDOW][:TOP]
    raises = [
        (known[task_id], _work(scores[task_id]))
        for task_id in top
        if known[task_id].get("rank") not in WINDOW[1:] and task_id not in fixed
    ]
    if config.autonomy in APPLIES:
        mine = owned | (claimable - fixed)
        held = partial(_claimable_lowered, known, doc, graph, fix)
        stale = [t for t in doc["tasks"] if t["id"] in mine and _open(t) and _lowerable(t, held)]
        lowers = [(t, _why(t["id"], queue, scores, claimable)) for t in stale if t["id"] not in top]
        steps = [partial(_lower, slug, ledger, *row) for row in lowers]
        steps += [partial(_raise, slug, ledger, *row) for row in raises]
    else:
        mail = Mail(InboxStore(store.redis), store, slug)
        steps = [partial(_propose, slug, ledger, mail, *row) for row in raises]
    done = []
    try:
        for step in steps:
            if action := step():
                done.append(action)
    finally:
        errors = log(slug, done, now_ms)
    return [action.text for action in done] + errors


def _raise(slug, ledger, task, work):
    text = f"ranked task {task['id']} high: it unblocks {work}"
    return _rank(slug, ledger, task, RANK, text, RAISED.format(work=work))


def _lower(slug, ledger, task, why):
    text = f"lowered task {task['id']} to normal: {why}"
    return _rank(slug, ledger, task, LOWER, text, LOWERED.format(why=why))


def _rank(slug, ledger, task, rank, text, comment):
    try:
        ledger.rank_task(slug, task["id"], rank, AUTHOR, if_owned=True)
    except LedgerRefused:
        return None
    task["rank"] = rank
    return Action(LEVERAGE, "apply", text + _comment(slug, ledger, task, comment), task)


def _why(task_id, queue, scores, claimable):
    if task_id in queue:
        return OUTRANKED.format(work=_work(scores[task_id]), top=TOP)
    return IDLE if task_id in claimable else UNREADY


def _lowerable(task, claimable_at_lower):
    return task.get("rank") == RANK or task.get("rank") == "urgent" and claimable_at_lower(task)


def _claimable_lowered(known, doc, graph, fix, task):
    from scripts.swarm import tick

    return tick.ready({**task, "rank": LOWER}, known, doc, graph, fix)


def _comment(slug, ledger, task, text):
    try:
        ledger.comment(slug, task["id"], text, AUTHOR)
    except SwarmError:
        return UNCOMMENTED
    return ""


def _propose(slug, ledger, mail, task, work):
    text = PROPOSE.format(slug=slug, task=task["id"], title=task["title"], work=work)
    if not mail.send(f"dispatch-rank:{task['id']}", mail.master, text):
        return None
    said = f"proposed rank high for task {task['id']} to the master: it unblocks {work}"
    return Action(LEVERAGE, "propose", said + _comment(slug, ledger, task, PROPOSED.format(work=work)), task)


def group(slug, config, store, ledger, doc, now_ms):
    mode = "apply" if config.autonomy in grouping.APPLIES else "propose"
    found = grouping.group_pass(slug, config, store, ledger, doc)
    landed = [Action(GROUPING, mode, text, {}) for text in found if not text.startswith(SKIPPED)]
    return found + log(slug, landed, now_ms)


def priorities(store, slug, doc, ledger, view, now_ms):
    found = priority_sweep.priority_pass(store, slug, doc, ledger, None, view)
    return found + log(slug, [Action(PRIORITIES, "apply", text, {}) for text in found], now_ms)


def log(slug, actions, now_ms):
    from scripts.swarm import metrics

    if not actions:
        return []
    for action in actions:
        row = gate_log.Row(now_ms, AUTHOR, action.mode, AUTHOR, action.task.get("id", ""), action.rule, action.text)
        gate_log.append(slug, row)
    rows = [dispatch_row(slug, index, action, now_ms) for index, action in enumerate(actions)]
    return metrics.record(TABLE, rows, now_ms)


def dispatch_row(slug, index, action, now_ms):
    task = action.task
    return {
        "event_id": f"dispatch:{slug}:{action.rule}:{now_ms}:{index}",
        "ledger": slug,
        "ts_ms": now_ms,
        "plan": task.get("plan_url", ""),
        "phase": task.get("phase", ""),
        "slice": task.get("plan_slice", ""),
        "task": task.get("id", ""),
        "rule": action.rule,
        "mode": action.mode,
        "action": action.text,
    }


def _open(task):
    return task.get("state") != "done" and not task.get("out_of_scope")


def _owned(doc):
    return _ranked_by(doc, AUTHOR)


def _fixed(doc):
    return _ranked_by(doc, ledger_rank.OPERATOR)


def _ranked_by(doc, author):
    stamps = doc.get("_meta", {}).get("stamps", {})
    return {
        t["id"] for t in doc["tasks"] if "rank" in t and stamps.get(f"tasks/{t['id']}/rank", {}).get("by") == author
    }


def _lineage(node, nodes):
    seen = []
    while node in nodes and node not in seen:
        seen.append(node)
        node = nodes[node][1]
    return seen


def _downstream(task_id, waiters):
    found, stack = set(), [task_id]
    while stack:
        for waiter in waiters[stack.pop()]:
            if waiter not in found and waiter != task_id:
                found.add(waiter)
                stack.append(waiter)
    return found


def _focus_order(doc):
    targets = [row.get("target") for row in doc.get("freezes", []) if row.get("verb") == "focus"]
    nodes, _ = hierarchy.project(doc)
    order = {}
    for t in doc["tasks"]:
        covers = {*_lineage(f"tasks/{t['id']}", nodes), f"lane:{t.get('lane')}", f"kind:{t.get('kind', 'code')}"}
        order[t["id"]] = next((index for index, target in enumerate(targets) if target in covers), len(targets))
    return order


def _work(count):
    return f"{count} open task" if count == 1 else f"{count} open tasks"
