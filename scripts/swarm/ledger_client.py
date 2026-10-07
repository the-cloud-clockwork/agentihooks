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
            detail = "; ".join(state.get("_meta", {}).get("warnings", [])) or str(state["rejected"])
            raise SwarmError(f"ledger {slug} refused: {detail}")
        return state

    def state(self, slug):
        return self._call(slug)

    def tasks(self, slug):
        return self._call(slug).get("tasks", [])

    def events(self, slug):
        return self._call(slug).get("_meta", {}).get("events", [])

    def chat(self, slug):
        return self._call(slug).get("chat", [])

    def update_task(self, slug, task_id, fields, by="swarm", if_state=()):
        guard = {"if_state": list(if_state)} if if_state else {}
        state = self._call(slug, [_op("task_update", by, item=f"tasks/{task_id}", fields=fields, **guard)])
        return next((t for t in state.get("tasks", []) if t["id"] == task_id), {})

    def rename_agent(self, slug, old, new):
        self._call(slug, [_op("agent_rename", "swarm", old=old, new=new)])

    def add_task(self, slug, fields, by):
        self._call(slug, [{**_op("task_add", by), **fields}])

    def comment(self, slug, task_id, text, by):
        self._call(slug, [_op("add", by, thread=f"tasks/{task_id}/comments", text=text)])

    def set_phase(self, slug, phase_id, done, status):
        self._call(slug, [_op("set", "swarm", path=f"phases/{phase_id}/done", value=done, status=status)])

    def comment_phase(self, slug, phase_id, text, by):
        self._call(slug, [_op("add", by, thread=f"phases/{phase_id}/comments", text=text)])

    def review_phase(self, slug, phase_id, state, by="swarm", note="", override=None):
        fields = {"item": f"phases/{phase_id}", "state": state, **({"note": note} if note else {})}
        if override:
            fields["override"] = override
        self._call(slug, [_op("phase_review", by, **fields)])

    def say(self, slug, text, by=None):
        op = _op("add", by, thread="chat", text=text)
        if by is None:
            op.pop("by")
        self._call(slug, [op])

    def notify(self, slug, text):
        self.say(slug, text, by="swarm")

    def followup(self, slug, text):
        self._call(slug, [_op("add_item", "swarm", list="followups", text=text)])

    def priority(self, slug, item, text):
        self._call(slug, [_op("priority", "swarm", item=item, text=text)])

    def clear_priority(self, slug, priority_id, reason):
        self._call(slug, [_op("priority_clear", "swarm", target=priority_id, reason=reason)])

    def comment_item(self, slug, item, text):
        self._call(slug, [_op("add", "swarm", thread=f"{item}/comments", text=text)])

    def mark_done(self, slug, item):
        self._call(slug, [_op("set", "swarm", path=f"{item}/done", value=True)])

    def answer_as_operator(self, slug, question, text):
        op = _op("add", None, thread=f"{question}/answers", text=text)
        op.pop("by")
        self._call(slug, [op])

    def add_source(self, slug, source, by):
        self._call(slug, [_op("source_add", by, source=source)])

    def summarize(self, slug, note, by):
        self._call(slug, [_op("summary_set", by, note=note)])

    def mark_closed(self, slug, by):
        self._call(slug, [_op("close", by)])

    def mark_swarm(self, slug, by="swarm"):
        self._call(slug, [_op("size_set", by, size="swarm")])

    def reopen(self, slug, by):
        self._call(slug, [_op("reopen", by)])
        _ledger()
        import ledger_bin

        ledger_bin.restore(slug)

    def bin_closed(self, slug, closed_at):
        _ledger()
        import ledger_bin

        return ledger_bin.bin_closed(slug, closed_at)

    def closed(self, slug):
        return bool(self._call(slug).get("closed_at"))

    def binned(self, slug):
        _ledger()
        import ledger_bin

        return slug in ledger_bin.entries()


def _op(kind, by, **fields):
    return {"op": kind, "id": f"{kind}-{uuid.uuid4().hex[:10]}", "by": by, **fields}
