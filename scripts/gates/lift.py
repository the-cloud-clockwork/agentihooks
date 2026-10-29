"""The operator's lift: 'lift the <name> gate' typed in an agent's pane lets that gate's denies through for one hour.

A swarm lift on an agent holding a task binds the task: every agent on it passes until its pull request head moves.
"""

import json
import re
import time
import uuid

from scripts.gates import log
from scripts.gates.log import safe_name

LIFT_SECONDS = 3600
SERVER_GATES = frozenset({"talk"})
LIFT_REASON = "the operator lifted it for one hour"
HEAD_REASON = "the operator lifted it for this task until its pull request head moves"
SIGNAL = re.compile(r"\blift\s+(?:the\s+)?([a-z][a-z0-9-]*)\s+gate\b", re.IGNORECASE)


def requested(prompt):
    return {match.group(1).lower() for match in SIGNAL.finditer(prompt)}


def lift_path(slug, session_id, gate, home=None):
    return log.gates_dir(slug, home) / "lifts" / safe_name(session_id) / safe_name(gate)


def arm(slug, session_id, gate, home=None, now=None):
    path = lift_path(slug, session_id, gate, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": time.time() if now is None else now}))


def lifted(slug, session_id, gate, home=None, now=None):
    if not (slug and session_id):
        return False
    return _fresh(lift_path(slug, session_id, gate, home), now)


def _shared(home):
    import redis

    from scripts.swarm.store import SwarmError, connect

    try:
        return connect()
    except (redis.RedisError, SwarmError):
        if home is None:
            raise
        return None


def _load(slug, scope, subject, gate, home):
    import redis

    from scripts.swarm.store import SwarmError

    try:
        store = _shared(home)
        if store is not None:
            return store.gate_lift(slug, scope, subject, gate)
    except (redis.RedisError, SwarmError, ValueError):
        return {}
    path = (
        agent_lift_path(slug, subject, gate, home) if scope == "agents" else task_lift_path(slug, subject, gate, home)
    )
    return _record(path)


def _save(slug, scope, subject, gate, record, home):
    store = _shared(home)
    if store is not None:
        store.put_gate_lift(slug, scope, subject, gate, record)
        return
    path = (
        agent_lift_path(slug, subject, gate, home) if scope == "agents" else task_lift_path(slug, subject, gate, home)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


def agent_lift_path(slug, agent, gate, home=None):
    return log.gates_dir(slug, home) / "lifts" / "agents" / safe_name(agent) / safe_name(gate)


def agent_lifted(slug, agent, gate, home=None, now=None):
    if not (slug and agent):
        return False
    return _alive(_load(slug, "agents", agent, gate, home), now)


def task_lifts_dir(slug, task, home=None):
    return log.gates_dir(slug, home) / "lifts" / "tasks" / safe_name(task)


def task_lift_path(slug, task, gate, home=None):
    return task_lifts_dir(slug, task, home) / safe_name(gate)


def task_lifted(slug, task, gate, home=None, now=None, command=""):
    if not (slug and task):
        return False
    record = _load(slug, "tasks", task, gate, home)
    if not record.get("url"):
        return _alive(record, now)
    from scripts.gates.identity import simple_commands
    from scripts.gates.intent import _merge_target, _same_pull, pr_head, production_unchanged

    targets = filter(None, map(_merge_target, simple_commands(command)))
    if not all(_same_pull(target, record["url"]) for target in targets):
        return False
    if not record.get("head"):
        return False
    head = pr_head(record["url"])
    return head == record["head"] or (bool(head) and production_unchanged(record["url"], record["head"], head))


def head_lifted(slug, task, gate, home=None):
    return bool(_load(slug, "tasks", task, gate, home).get("url")) and task_lifted(slug, task, gate, home)


def drop_task(slug, task, home=None, store=None):
    store = _shared(home) if store is None else store
    if store is not None:
        store.drop_task_lifts(slug, task)
    for path in task_lifts_dir(slug, task, home).glob("*"):
        path.unlink()


def held(slug, agent, task, gate, home=None, command=""):
    if task and gate not in SERVER_GATES:
        return task_lifted(slug, task, gate, home, command=command)
    return agent_lifted(slug, agent, gate, home)


def lift_agent(who, gate, home=None, now=None, pull=None):
    at = time.time() if now is None else now
    _save(who.swarm, "agents", who.name, gate, {"at": at}, home)
    if who.swarm and who.task and gate not in SERVER_GATES:
        _save(who.swarm, "tasks", who.task, gate, {"at": at, **(pull or {})}, home)
    reason = HEAD_REASON if pull and gate not in SERVER_GATES else LIFT_REASON
    log.append(who.swarm, log.Row.of(gate, "lift", who, reason=reason), home)


def active(rows, now_ms):
    since = now_ms - LIFT_SECONDS * 1000
    gates = {}
    for row in rows:
        if row.get("kind") in ("deny", "lift") and row.get("agent") and row.get("at", 0) > since:
            key = (row["agent"], row.get("gate"))
            gates[key] = row["kind"] == "lift"
    by_agent = {}
    for (agent, gate), was_lifted in sorted(gates.items()):
        by_agent.setdefault(agent, []).append({"gate": gate, "lifted": was_lifted})
    return by_agent


def current(slug, now_ms, home=None):
    return active(log.since(slug, now_ms - LIFT_SECONDS * 1000, home), now_ms)


def _fresh(path, now):
    return _alive(_record(path), now)


def _record(path):
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _alive(record, now):
    at = record.get("at")
    if not isinstance(at, (int, float)):
        return False
    return (time.time() if now is None else now) - at < LIFT_SECONDS


def post(slug):
    from scripts.swarm.ledger_client import _ledger

    return lambda op: _ledger().request(slug, [op])


def arm_from_prompt(prompt, session_id, who, known, home=None, send=None):
    if not (who.pinned and session_id):
        return []
    armed = sorted(requested(prompt) & (set(known) | SERVER_GATES))
    if not armed:
        return []
    for gate in armed:
        arm(who.swarm, session_id, gate, home)
        log.append(who.swarm, log.Row.of(gate, "lift", who, reason=LIFT_REASON), home)
    send = send or post(who.swarm)
    for gate in armed:
        send({"op": "gate_lift", "id": f"gate_lift-{uuid.uuid4().hex[:10]}", "by": who.name, "gate": gate})
    return armed
