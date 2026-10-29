"""The dispatcher's tick step: the claim queue by critical path and work unblocked, grouping and the priority sweep.

Each tick it orders every claimable task in focus, leaves included, by the longest open dependency chain behind it, then
the open work it unblocks, the bottleneck lane, focus order and ledger position. A rank the master or the swarm set is a
default it reorders; only an operator rank, set by him or relayed with his quoted words, and an urgent rank, whoever
set it, order ahead of that queue, and an operator rank or a low rank the swarm set on work it generated, such as
documentation tasks, or a generated documentation task left unranked, orders behind it; that low work never leads the head, the chain guard or a proposal.
At delegate and full the tick claims from this queue, resumed work first within a rank band and clear territory before
overlapping, and the dispatcher writes no rank: it comments on a task only when the task enters the head of its lane's
queue, as many tasks as the lane has seats among those that unblock work or carry an operator rank, or leaves it while
still open; a lane with no seats keeps its head until seats return. When a task on the critical chain of the work no
freeze or focus holds merges, its claimable successor heads the queue ahead of every band and takes the next free seat
in its lane, and the dispatcher reports the seconds from that merge to the claim on the task. Below delegate it proposes
each raise to the master. Every action is a gate log row, a dispatch metrics row naming its rule and a comment on the
task.
"""

import json
from collections import Counter
from dataclasses import dataclass
from functools import partial

from scripts.gates import log as gate_log
from scripts.inbox.store import InboxStore
from scripts.swarm import (
    apply_wait,
    bottleneck,
    capacity,
    freeze,
    grouping,
    metrics_outbox,
    priority_sweep,
    release_wait,
)
from scripts.swarm.ledger_events import Mail
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger_rank
from scripts.swarm_ledger.repository import hierarchy

AUTHOR = "dispatcher"
SWARM = "swarm"
APPLIES = ("delegate", "full")
TOP = 3
HEAD = "dispatch-head"
LANES = {"engineering": "eng", "ci": "ci"}
LEVERAGE, QUEUE, GROUPING, PRIORITIES = "leverage-rank", "queue-head", "grouping", "priority-sweep"
CHAIN = "chain-guard"
GUARD = -1
CHAIN_CLAIM = "chain guard claimed task {task} {elapsed} seconds after task {dep} merged on the critical chain"
CHAIN_CLAIMED = (
    "The dispatcher's chain guard gave this task the next free {lane} seat {elapsed} seconds after the task before "
    "it on the critical chain merged."
)
SKIPPED = "skipped "
WINDOW = (None, "high", "urgent")
UNCOMMENTED = "; the ledger did not take its comment"
TABLE = metrics_outbox.Table("dispatch_actions", (("rule", "String"), ("mode", "String"), ("action", "String")))
ENTERED = "The dispatcher put this task at the head of the {lane} claim queue: {why}."
LEFT = "The dispatcher moved this task out of the head of the {lane} claim queue: {why}."
LEADS = "it leads a chain of {chain} and unblocks {work}"
OPERATOR_RANK = "the operator ranked it {rank}"
URGENT_RANK = "it is ranked urgent"
OUTRANKED = "it was outranked by {ahead} in focus with longer chains, more unblocked work or an operator rank"
ENTER = (ENTERED, "put task {} at the head of")
LEAVE = (LEFT, "moved task {} out of the head of")
UNREADY = "it is no longer a claimable task in focus"
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


def ordered(doc: dict, named: str, scores: dict, caps: dict | None = None, weight: int = 50) -> list[str]:
    lane, depth, focus = LANES.get(named), chains(doc), _focus_order(doc, caps, weight)
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


def queue(doc: dict, fix: str, caps: dict | None = None, weight: int = 50) -> list[str]:
    fixed, guard, known = _banded(doc), guarded(doc, fix), {t["id"]: t for t in doc["tasks"]}
    lined = ordered(doc, None, leverage(doc), caps, weight)
    return sorted(lined, key=lambda task_id: _band(known[task_id], fixed, guard))


def lane_caps(config) -> dict[str, int]:
    return {"eng": config.max_eng, "ci": config.max_ci, "plan": config.max_plan}


def claim_key(config, doc):
    if config.autonomy not in APPLIES:
        return None
    fix = freeze.fix_phase(config)
    fixed, guard = _banded(doc), guarded(doc, fix)
    lined = queue(doc, fix, lane_caps(config), config.focus_weight)
    position = {task_id: index for index, task_id in enumerate(lined)}
    return lambda task: (_band(task, fixed, guard), position.get(task["id"], len(lined)))


def guarded(doc: dict, fix: str) -> set[str]:
    graph, known, deferred = hierarchy.project(doc)[0], {t["id"]: t for t in doc["tasks"]}, _deferred(doc)
    depth = chains({**doc, "tasks": [t for t in doc["tasks"] if not freeze.claim_gate(t, doc, graph, fix)]})
    longest = depth and max(depth.values())
    return {
        task_id
        for task_id, length in depth.items()
        if length + 1 >= longest and _merged(known[task_id], known) and task_id not in deferred
    }


def _merged(task, known):
    return [dep for dep in task.get("depends_on") or [] if known.get(dep, {}).get("state") == "done"]


def chain_claimed(slug, config, ledger, doc, task, lane, now_ms):
    if config.autonomy not in APPLIES or task["id"] not in guarded(doc, freeze.fix_phase(config)):
        return []
    return _chain_report(slug, ledger, doc, task, lane, now_ms)


def _chain_report(slug, ledger, doc, task, lane, now_ms):
    known, stamps = {t["id"]: t for t in doc["tasks"]}, doc.get("_meta", {}).get("stamps", {})
    times = [
        (stamps[f"tasks/{dep}/state"]["at"], dep) for dep in _merged(task, known) if f"tasks/{dep}/state" in stamps
    ]
    if not times:
        return []
    merged_at, dep = max(times)
    elapsed = (now_ms - merged_at) // 1000
    text = CHAIN_CLAIM.format(task=task["id"], elapsed=elapsed, dep=dep)
    said = _comment(slug, ledger, task, CHAIN_CLAIMED.format(lane=lane, elapsed=elapsed))
    action = Action(CHAIN, "apply", text + said, task)
    return [action.text] + log(slug, [action], now_ms)


def _band(task, fixed, guard):
    if task["id"] in guard:
        return GUARD
    return ledger_rank.order(task) if task["id"] in fixed else ledger_rank.RANKS.index(ledger_rank.DEFAULT)


def rank_pass(slug, config, store, ledger, doc, now_ms):
    from scripts.swarm import readiness

    known, named = {t["id"]: t for t in doc["tasks"]}, bottleneck.read(store, slug).get("bottleneck")
    graph, fix = hierarchy.project(doc)[0], freeze.fix_phase(config)
    released, applied = release_wait.checker(config.repo), apply_wait.checker(config, known)
    deferred = _deferred(doc)
    claimable = {
        task_id
        for task_id, t in known.items()
        if task_id not in deferred and readiness.ready(t, known, doc, graph, fix) and released(t) and applied(t)
    }
    scores, fixed = leverage(doc), _fixed(doc)
    if config.autonomy in APPLIES:
        before, banded = _saved_heads(store, slug), _banded(doc)
        lined_queue = queue(doc, fix, lane_caps(config), config.focus_weight)
        leading = [t for t in lined_queue if t in claimable and (scores[t] or t in banded)]
        heads = _heads(store, slug, config, leading, known, before)
        landed = {
            lane: [t for t in before.get(lane, []) if t in head or known.get(t, {}).get("state") == "open"]
            for lane, head in heads.items()
        }
        led = set(leading)
        standing = {task_id: task_id in led for task_id in claimable}
        steps = _head_steps(slug, ledger, doc, before, heads, standing, landed)
    else:
        lined_order = ordered(doc, named, scores, lane_caps(config), config.focus_weight)
        lined = [t for t in lined_order if t in claimable and scores[t]]
        top = [t for t in lined if t not in fixed or known[t].get("rank") in WINDOW][:TOP]
        raises = [
            (known[t], _work(scores[t])) for t in top if known[t].get("rank") not in WINDOW[1:] and t not in fixed
        ]
        mail = Mail(InboxStore(store.redis), store, slug)
        steps, landed = [partial(_propose, slug, ledger, mail, *row) for row in raises], None
    done = []
    try:
        for step in steps:
            if action := step():
                done.append(action)
    finally:
        errors = log(slug, done, now_ms)
        if landed is not None:
            store.redis.set(store.key(slug, HEAD), json.dumps(landed))
    return [action.text for action in done] + errors


def _heads(store, slug, config, lined, known, before):
    seats = capacity.caps(capacity.read(store, slug), config)
    return {
        lane: [t for t in lined if known[t].get("lane") == lane][:cap] if cap else before.get(lane, [])
        for lane, cap in seats.items()
    }


def _saved_heads(store, slug):
    saved = store.redis.get(store.key(slug, HEAD))
    return json.loads(saved) if saved else {}


def _head_steps(slug, ledger, doc, before, heads, standing, landed):
    known, depth, scores, fixed = {t["id"]: t for t in doc["tasks"]}, chains(doc), leverage(doc), _fixed(doc)
    steps = []
    for lane, head in heads.items():
        earlier = before.get(lane, [])
        why = partial(_left_why, standing, len(head))
        steps += [
            partial(_head, slug, ledger, lane, known[t], LEAVE, why(t), landed)
            for t in earlier
            if t not in head and known.get(t, {}).get("state") == "open"
        ]
        steps += [
            partial(_head, slug, ledger, lane, known[t], ENTER, _entered_why(known[t], fixed, depth, scores), landed)
            for t in head
            if t not in earlier
        ]
    return steps


def _entered_why(task, fixed, depth, scores):
    if task["id"] in fixed:
        return OPERATOR_RANK.format(rank=task["rank"])
    if task.get("rank") == "urgent":
        return URGENT_RANK
    return LEADS.format(chain=_work(depth[task["id"]]), work=_work(scores[task["id"]]))


def _left_why(standing, seats, task_id):
    ahead = "1 claimable task" if seats == 1 else f"{seats} claimable tasks"
    if task_id not in standing:
        return UNREADY
    return OUTRANKED.format(ahead=ahead) if standing[task_id] else IDLE


def _head(slug, ledger, lane, task, kind, why, landed):
    template, moved = kind
    text = f"{moved.format(task['id'])} the {lane} claim queue: {why}"
    action = Action(QUEUE, "apply", text + _comment(slug, ledger, task, template.format(lane=lane, why=why)), task)
    if kind == ENTER:
        landed[lane].append(task["id"])
    else:
        landed[lane].remove(task["id"])
    return action


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


def _fixed(doc):
    urgent = {t["id"] for t in doc["tasks"] if t.get("rank") == "urgent"}
    return _ranked_by(doc, ledger_rank.OPERATOR) | (_ranked_by(doc, AUTHOR) & urgent)


def _banded(doc):
    urgent = {t["id"] for t in doc["tasks"] if t.get("rank") == "urgent"}
    return _fixed(doc) | _deferred(doc) | urgent


def _deferred(doc):
    low = {t["id"] for t in doc["tasks"] if t.get("rank") == ledger_rank.RANKS[-1]}
    unranked = {t["id"] for t in doc["tasks"] if t.get("rank") not in ledger_rank.RANKS and ledger_rank.generated(t)}
    return (_ranked_by(doc, SWARM) & low) | unranked


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


HOLDING_STATES = ("claimed", "pr")


def _focus_order(doc, caps=None, weight=50):
    """Focused tasks sort first, except in a lane already at its focus-weight share of claimed/pr tasks."""
    targets = [row.get("target") for row in doc.get("freezes", []) if row.get("verb") == "focus"]
    nodes, _ = hierarchy.project(doc)
    tasks = doc["tasks"]

    def match(t):
        covers = {*_lineage(f"tasks/{t['id']}", nodes), f"lane:{t.get('lane')}", f"kind:{t.get('kind', 'code')}"}
        return next((index for index, target in enumerate(targets) if target in covers), None)

    matched = {t["id"]: match(t) for t in tasks}
    holding = Counter(t.get("lane") for t in tasks if t.get("state") in HOLDING_STATES and matched[t["id"]] is not None)
    cap = {lane: (count * weight) // 100 for lane, count in (caps or {}).items()}
    order = {}
    for t in tasks:
        index, lane = matched[t["id"]], t.get("lane")
        over_cap = lane in cap and holding.get(lane, 0) >= cap[lane]
        order[t["id"]] = len(targets) if index is None or over_cap else index
    return order


def _work(count):
    return f"{count} open task" if count == 1 else f"{count} open tasks"
