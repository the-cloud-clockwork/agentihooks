"""The dispatcher's tick step: rank by the open work each task unblocks, grouping and the priority sweep, by autonomy.

At delegate and full it raises the top leverage tasks to high and never changes a rank someone set; below delegate it
proposes each to the master. Every action is a gate log row, a dispatch metrics row naming its rule and, for a rank, a
comment on the task.
"""

from dataclasses import dataclass

from scripts.gates import log as gate_log
from scripts.inbox.store import InboxStore
from scripts.swarm import bottleneck, grouping, metrics_outbox, priority_sweep
from scripts.swarm.ledger_events import Mail
from scripts.swarm_ledger.repository import hierarchy

AUTHOR = "dispatcher"
APPLIES = ("delegate", "full")
TOP = 3
RANK = "high"
LANES = {"engineering": "eng", "ci": "ci"}
LEVERAGE, GROUPING, PRIORITIES = "leverage-rank", "grouping", "priority-sweep"
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
    tasks = {t["id"]: t for t in doc.get("tasks", []) if _open(t)}
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


def ordered(doc: dict, named: str) -> list[str]:
    scores, lane = leverage(doc), LANES.get(named)
    focused = _focused(doc)
    position = {t["id"]: index for index, t in enumerate(doc.get("tasks", []))}
    rows = [t for t in doc.get("tasks", []) if scores.get(t["id"]) and t.get("state") == "open"]
    rows = [t for t in rows if not t.get("merged_into")]
    return [
        t["id"]
        for t in sorted(
            rows,
            key=lambda t: (-scores[t["id"]], t.get("lane") != lane, t["id"] not in focused, position[t["id"]]),
        )
    ]


def rank_pass(slug, config, store, ledger, doc, now_ms):
    scores, known = leverage(doc), {t["id"]: t for t in doc.get("tasks", [])}
    top = [known[task_id] for task_id in ordered(doc, bottleneck.read(store, slug).get("bottleneck", ""))[:TOP]]
    unranked = [(task, _work(scores[task["id"]])) for task in top if "rank" not in task]
    if config.autonomy in APPLIES:
        done = [_raise(slug, ledger, task, work) for task, work in unranked]
    else:
        mail = Mail(InboxStore(store.redis), store, slug)
        done = [action for task, work in unranked if (action := _propose(slug, ledger, mail, task, work))]
    return log(slug, done, now_ms)


def _raise(slug, ledger, task, work):
    ledger.rank_task(slug, task["id"], RANK, AUTHOR)
    ledger.comment(slug, task["id"], RAISED.format(work=work), AUTHOR)
    return Action(LEVERAGE, "apply", f"ranked task {task['id']} high: it unblocks {work}", task)


def _propose(slug, ledger, mail, task, work):
    text = PROPOSE.format(slug=slug, task=task["id"], title=task.get("title", ""), work=work)
    if not mail.send(f"dispatch-rank:{task['id']}", mail.master, text):
        return None
    ledger.comment(slug, task["id"], PROPOSED.format(work=work), AUTHOR)
    return Action(
        LEVERAGE, "propose", f"proposed rank high for task {task['id']} to the master: it unblocks {work}", task
    )


def group(slug, config, store, ledger, doc, now_ms):
    mode = "apply" if config.autonomy in grouping.APPLIES else "propose"
    found = grouping.group_pass(slug, config, store, ledger, doc)
    return log(slug, [Action(GROUPING, mode, text, {}) for text in found], now_ms)


def priorities(store, slug, doc, ledger, view, now_ms):
    found = priority_sweep.priority_pass(store, slug, doc, ledger, None, view)
    return log(slug, [Action(PRIORITIES, "apply", text, {}) for text in found], now_ms)


def log(slug, actions, now_ms):
    from scripts.swarm import metrics

    if not actions:
        return []
    for action in actions:
        row = gate_log.Row(now_ms, AUTHOR, action.mode, AUTHOR, action.task.get("id", ""), action.rule, action.text)
        gate_log.append(slug, row)
    rows = [dispatch_row(slug, index, action, now_ms) for index, action in enumerate(actions)]
    return [action.text for action in actions] + metrics.record(TABLE, rows, now_ms)


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
        for t in doc.get("tasks", [])
        if targets
        & {*_lineage(f"tasks/{t['id']}", nodes), f"lane:{t.get('lane', '')}", f"kind:{t.get('kind', 'code')}"}
    }


def _work(count):
    return f"{count} open task" if count == 1 else f"{count} open tasks"
