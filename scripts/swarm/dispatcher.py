"""The dispatcher's tick step: rank by the open work each task unblocks, grouping and the priority sweep, by autonomy.

At delegate and full it raises the top leverage tasks to high and never changes a rank someone set; below delegate it
proposes each to the master. Every action is a gate log row, a dispatch metrics row naming its rule and, for a rank, a
comment on the task.
"""

from dataclasses import dataclass
from functools import partial

from scripts.gates import log as gate_log
from scripts.inbox.store import InboxStore
from scripts.swarm import bottleneck, grouping, metrics_outbox, priority_sweep
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.ledger_events import Mail
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger.repository import hierarchy

AUTHOR = "dispatcher"
APPLIES = ("delegate", "full")
TOP = 3
RANK = "high"
LANES = {"engineering": "eng", "ci": "ci"}
LEVERAGE, GROUPING, PRIORITIES = "leverage-rank", "grouping", "priority-sweep"
SKIPPED = "skipped "
WINDOW = (None, "high", "urgent")
UNCOMMENTED = "; the ledger did not take its comment"
TABLE = metrics_outbox.Table("dispatch_actions", (("rule", "String"), ("mode", "String"), ("action", "String")))
RAISED = "The dispatcher raised this task to high rank because it unblocks {work}."
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
    return {task_id: len(_downstream(task_id, waiters)) for task_id in tasks}


def ordered(doc: dict, named: str, scores: dict) -> list[str]:
    lane = LANES.get(named)
    focused = _focused(doc)
    position = {t["id"]: index for index, t in enumerate(doc["tasks"])}
    rows = [t for t in doc["tasks"] if scores.get(t["id"]) and t.get("state") == "open"]
    rows = [t for t in rows if not t.get("merged_into")]
    return [
        t["id"]
        for t in sorted(
            rows,
            key=lambda t: (-scores[t["id"]], t.get("lane") != lane, t["id"] not in focused, position[t["id"]]),
        )
    ]


def rank_pass(slug, config, store, ledger, doc, now_ms):
    scores, known = leverage(doc), {t["id"]: t for t in doc["tasks"]}
    named = bottleneck.read(store, slug).get("bottleneck")
    window = [known[task_id] for task_id in ordered(doc, named, scores) if known[task_id].get("rank") in WINDOW]
    unranked = [(task, _work(scores[task["id"]])) for task in window[:TOP] if "rank" not in task]
    if config.autonomy in APPLIES:
        step = partial(_raise, slug, ledger)
    else:
        step = partial(_propose, slug, ledger, Mail(InboxStore(store.redis), store, slug))
    done = []
    try:
        for task, work in unranked:
            if action := step(task, work):
                done.append(action)
    finally:
        errors = log(slug, done, now_ms)
    return [action.text for action in done] + errors


def _raise(slug, ledger, task, work):
    try:
        ledger.rank_task(slug, task["id"], RANK, AUTHOR, if_unranked=True)
    except LedgerRefused:
        return None
    task["rank"] = RANK
    text = f"ranked task {task['id']} high: it unblocks {work}"
    return Action(LEVERAGE, "apply", text + _comment(slug, ledger, task, RAISED.format(work=work)), task)


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


def _focused(doc):
    targets = {row.get("target") for row in doc.get("freezes", []) if row.get("verb") == "focus"}
    nodes, _ = hierarchy.project(doc)
    return {
        t["id"]
        for t in doc["tasks"]
        if targets & {*_lineage(f"tasks/{t['id']}", nodes), f"lane:{t.get('lane')}", f"kind:{t.get('kind', 'code')}"}
    }


def _work(count):
    return f"{count} open task" if count == 1 else f"{count} open tasks"
