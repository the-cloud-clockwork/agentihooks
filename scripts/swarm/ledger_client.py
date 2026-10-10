"""The swarm's view of a swarm ledger: read tasks, write task fields, comments and chat over the ledger HTTP API."""

import sys
import uuid
from pathlib import Path

from scripts.swarm import notice_text
from scripts.swarm.store import SwarmError

LEDGER_DIR = Path(__file__).resolve().parents[1] / "swarm_ledger"
SERVICE_AUTHORS = ("swarm", None)


class LedgerGone(SwarmError):
    pass


class LedgerRefused(SwarmError):
    pass


def _ledger():
    if str(LEDGER_DIR) not in sys.path:
        sys.path.insert(0, str(LEDGER_DIR))
    import ledger

    return ledger


class LedgerClient:
    def __init__(self, service=False):
        self.service = service

    def _call(self, slug, ops=None):
        from scripts.swarm import lease

        if ops is not None and (epoch := lease.EPOCH.get()) is not None:
            ops = [{**op, "controller_epoch": epoch} for op in ops]
        service = self.service or all(op.get("by") in SERVICE_AUTHORS for op in ops or ())
        ledger = _ledger()
        try:
            state = ledger.call(slug, ops, service=service)
        except ledger.Missing as exc:
            raise LedgerGone(str(exc)) from exc
        except SystemExit as exc:
            refused = str(exc).startswith("server refused: 4")
            raise (LedgerRefused if refused else SwarmError)(f"ledger {slug}: {exc}") from exc
        if state.get("rejected"):
            detail = "; ".join(state.get("_meta", {}).get("warnings", [])) or str(state["rejected"])
            raise LedgerRefused(f"ledger {slug} refused: {detail}")
        return state

    def _resource(self, slug, path, collection=False):
        return _ledger().resource(slug, path, service=self.service, collection=collection)

    def state(self, slug):
        return self._call(slug)

    def metadata(self, slug):
        return self._resource(slug, "metadata")

    def tasks(self, slug):
        return self._resource(slug, "tasks", collection=True)

    def events(self, slug):
        return self._resource(slug, "events", collection=True)

    def chat(self, slug):
        return self._resource(slug, "chat", collection=True)

    def hierarchy(self, slug):
        return self._resource(slug, "hierarchy")

    def update_task(self, slug, task_id, fields, by="swarm", if_state=()):
        guard = {"if_state": list(if_state)} if if_state else {}
        state = self._call(slug, [_op("task_update", by, item=f"tasks/{task_id}", fields=fields, **guard)])
        return next((t for t in state.get("tasks", []) if t["id"] == task_id), {})

    def rename_agent(self, slug, old, new):
        self._call(slug, [_op("agent_rename", "swarm", old=old, new=new)])

    def add_task(self, slug, fields, by):
        self._call(slug, [{**_op("task_add", by), **fields}])

    def _notice(self, slug, op, kind):
        op = {**op, "text": notice_text.plain(op["text"], kind)}
        try:
            self._call(slug, [op])
        except LedgerRefused as exc:
            print(f"swarm notice dropped, the ledger refused it: {exc}", file=sys.stderr)
            return False
        return True

    def _write(self, slug, op, kind="comment"):
        if op.get("by") == "swarm":
            return self._notice(slug, op, kind)
        else:
            self._call(slug, [op])

    def comment(self, slug, task_id, text, by):
        self._write(slug, _op("add", by, thread=f"tasks/{task_id}/comments", text=text))

    def capacity_comment(self, slug: str, task_id: str, text: str, at: int) -> None:
        self.comment(slug, task_id, text, by="swarm")

    def time_left(self, slug: str, slots: int, ci_minutes: float | None) -> None:
        self._call(slug, [_op("time_left", "swarm", slots=slots, ci_minutes=ci_minutes)])

    def ack_events(self, slug: str, revision: int) -> None:
        self._call(slug, [_op("events_ack", "swarm", rev=revision)])

    def set_phase(self, slug, phase_id, done, status):
        self._call(slug, [_op("set", "swarm", path=f"phases/{phase_id}/done", value=done, status=status)])

    def comment_phase(self, slug, phase_id, text, by):
        self._write(slug, _op("add", by, thread=f"phases/{phase_id}/comments", text=text))

    def review_phase(self, slug, phase_id, state, by="swarm", note="", override=None):
        fields = {"item": f"phases/{phase_id}", "state": state, **({"note": note} if note else {})}
        if override:
            fields["override"] = override
        self._call(slug, [_op("phase_review", by, **fields)])

    def say(self, slug, text, by=None):
        op = _op("add", by, thread="chat", text=text, to="operator")
        if by is None:
            op.pop("by")
            op.pop("to")
        self._call(slug, [op])

    def relay(self, slug, text, by):
        LedgerClient(service=True)._call(slug, [_op("add", by, thread="chat", text=text, to="operator")])

    def notify(self, slug, text):
        self._call(slug, [_op("notice", "swarm", text=text)])

    def followup(self, slug, text, needs_operator=False):
        flag = {"needs_operator": True} if needs_operator else {}
        return self._write(slug, _op("add_item", "swarm", list="followups", text=text, **flag), "item")

    def priority(self, slug, item, text):
        return self._write(slug, _op("priority", "swarm", item=item, text=text), "priority")

    def group_tasks(self, slug, lead, members):
        self._call(slug, [_op("task_group", "swarm", item=f"tasks/{lead}", members=list(members))])

    def ungroup_tasks(self, slug, lead):
        self._call(slug, [_op("task_ungroup", "swarm", item=f"tasks/{lead}")])

    def clear_priority(self, slug, priority_id, reason):
        self._call(slug, [_op("priority_clear", "swarm", target=priority_id, reason=reason)])

    def comment_item(self, slug, item, text):
        self._write(slug, _op("add", "swarm", thread=f"{item}/comments", text=text))

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

    def join(self, slug, name, role):
        self._call(slug, [_op("join", name, role=role)])

    def bin_closed(self, slug, closed_at):
        _ledger()
        import ledger_bin

        return ledger_bin.bin_closed(slug, closed_at)

    def closed(self, slug):
        return bool(self._resource(slug, "metadata").get("closed_at"))

    def binned(self, slug):
        _ledger()
        import ledger_bin

        return slug in ledger_bin.entries()


def _op(kind, by, **fields):
    return {"op": kind, "id": f"{kind}-{uuid.uuid4().hex[:10]}", "by": by, **fields}
