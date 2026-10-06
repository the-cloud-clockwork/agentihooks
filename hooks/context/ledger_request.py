"""The operator's request on a swarm agent's own task: his comment on the ledger page, or the master's relay of
words he typed in the master pane, honoured while that master session still holds them."""

import json
import os
import time
from pathlib import Path

from hooks.context import operator_words

COMMENT_SEC = 3600
RELAY_SEC = 1800


def _ledger(env):
    slug = env.get("AGENTIHOOKS_SWARM")
    if not slug:
        return {}
    folder = Path(env.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()
    try:
        return json.loads((folder / f"{slug}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _relayed(entry, ledger, asks, now):
    from scripts.swarm.naming import addresses

    by = entry["relayed_by"]
    masters = {name for name, member in ledger["_meta"]["members"].items() if member.get("role") == "orchestrator"}
    if by not in masters:
        return False
    return any(asks(operator_words.matching(name, entry["quote"], now, RELAY_SEC)) for name in addresses(by))


def find(asks, environ=None, now=None):
    """('ledger' or 'relay', comment id) for the newest operator comment on this session's task that asks, else None."""
    env = os.environ if environ is None else environ
    now = time.time() if now is None else now
    ledger = _ledger(env)
    task_id = env.get("AGENTIHOOKS_SWARM_TASK")
    task = next((t for t in ledger.get("tasks", []) if t["id"] == task_id), {"comments": []})
    for entry in reversed(task["comments"]):
        if entry["by"] != "operator" or entry.get("deleted"):
            continue
        if "relayed_by" in entry:
            if _relayed(entry, ledger, asks, now):
                return "relay", entry["id"]
        elif now - entry["at"] / 1000 < COMMENT_SEC and asks(entry["text"]):
            return "ledger", entry["id"]
    return None
