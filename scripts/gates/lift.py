"""The operator's lift: 'lift the <name> gate' typed in an agent's pane lets that gate's denies through for one hour."""

import json
import re
import time
import uuid

from scripts.gates import log
from scripts.gates.verdicts import safe_name

LIFT_SECONDS = 3600
LIFT_REASON = "the operator lifted it for one hour"
SIGNAL = re.compile(r"\blift\s+(?:the\s+)?([a-z][a-z0-9-]*)\s+gate\b", re.IGNORECASE)


def requested(prompt):
    return {match.group(1).lower() for match in SIGNAL.finditer(prompt or "")}


def lift_path(slug, session_id, gate, home=None):
    return log.gates_dir(slug, home) / "lifts" / safe_name(session_id) / gate


def arm(slug, session_id, gate, home=None, now=None):
    path = lift_path(slug, session_id, gate, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": time.time() if now is None else now}), encoding="utf-8")


def lifted(slug, session_id, gate, home=None, now=None):
    if not (slug and session_id):
        return False
    try:
        record = json.loads(lift_path(slug, session_id, gate, home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    return (time.time() if now is None else now) - record.get("at", 0) < LIFT_SECONDS


def post(slug):
    from scripts.swarm.ledger_client import _ledger

    return lambda op: _ledger().request(slug, [op])


def arm_from_prompt(prompt, session_id, who, known, home=None, send=None):
    if not (who.pinned and session_id):
        return []
    armed = sorted(requested(prompt) & set(known))
    if not armed:
        return []
    for gate in armed:
        arm(who.swarm, session_id, gate, home)
        log.append(who.swarm, log.Row.of(gate, "lift", who, reason=LIFT_REASON), home)
    send = send or post(who.swarm)
    for gate in armed:
        send({"op": "gate_lift", "id": f"gate_lift-{uuid.uuid4().hex[:10]}", "by": who.name, "gate": gate})
    return armed
