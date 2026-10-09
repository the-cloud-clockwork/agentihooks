"""The operator's request on a swarm agent's own task: his comment on the ledger page, or the master's relay of
words he typed in the master pane, honoured until that task is done or cancelled."""

import json
import os
from pathlib import Path


def _ledger(env):
    slug = env.get("AGENTIHOOKS_SWARM")
    if not slug:
        return {}
    folder = Path(env.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()
    try:
        return json.loads((folder / f"{slug}.json").read_bytes())
    except (OSError, ValueError):
        return {}


def _closed(task):
    return any(task.get(mark) for mark in ("done", "out_of_scope", "deleted"))


def _relayed(entry, ledger, asks):
    """The ledger server stores a relay only after checking its quote against the operator's typed words."""
    masters = {name for name, member in ledger["_meta"]["members"].items() if member.get("role") == "orchestrator"}
    return entry["relayed_by"] in masters and asks(entry["quote"])


def find(asks, environ=None):
    """('ledger' or 'relay', comment id) for the newest operator comment on this session's open task that asks, else None."""
    env = os.environ if environ is None else environ
    ledger = _ledger(env)
    task_id = env.get("AGENTIHOOKS_SWARM_TASK")
    task = next((t for t in ledger.get("tasks", []) if t["id"] == task_id), {"comments": []})
    if _closed(task):
        return None
    for entry in reversed(task["comments"]):
        if entry["by"] != "operator" or entry.get("deleted"):
            continue
        if "relayed_by" in entry:
            if _relayed(entry, ledger, asks):
                return "relay", entry["id"]
        elif asks(entry["text"]):
            return "ledger", entry["id"]
    return None
