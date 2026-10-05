"""The swarm's view of a swarm ledger: read tasks, write task fields, comments and chat over the ledger HTTP API."""

import sys
import uuid
from pathlib import Path

from scripts.swarm.store import SwarmError

LEDGER_DIR = Path(__file__).resolve().parents[1] / "swarm_ledger"


def _ledger():
    if str(LEDGER_DIR) not in sys.path:
        sys.path.insert(0, str(LEDGER_DIR))
    import ledger

    return ledger


class LedgerClient:
    def _call(self, slug, ops=None):
        try:
            state = _ledger().call(slug, ops)
        except SystemExit as exc:
            raise SwarmError(f"ledger {slug}: {exc}") from exc
        if state.get("rejected"):
            raise SwarmError(f"ledger {slug} refused: {state['rejected']}")
        return state

    def tasks(self, slug):
        return self._call(slug).get("tasks", [])

    def events(self, slug):
        return self._call(slug).get("_meta", {}).get("events", [])

    def chat(self, slug):
        return self._call(slug).get("chat", [])

    def update_task(self, slug, task_id, fields, by="swarm"):
        self._call(slug, [_op("task_update", by, item=f"tasks/{task_id}", fields=fields)])

    def comment(self, slug, task_id, text, by):
        self._call(slug, [_op("add", by, thread=f"tasks/{task_id}/comments", text=text)])

    def say(self, slug, text, by=None):
        op = _op("add", by, thread="chat", text=text)
        if by is None:
            op.pop("by")
        self._call(slug, [op])

    def notify(self, slug, text):
        self.say(slug, text, by="swarm")

    def followup(self, slug, text):
        self._call(slug, [_op("add_item", "swarm", list="followups", text=text)])


def _op(kind, by, **fields):
    return {"op": kind, "id": f"{kind}-{uuid.uuid4().hex[:10]}", "by": by, **fields}
