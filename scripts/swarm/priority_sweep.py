"""Priorities that clear themselves on the minute tick, so the operator never clears the list by hand.

The sweep clears an agent's priority whose item is done, out of scope, merged or gone. Then each new comment, answer or
chat line on an item that carries a priority asks the classifier whether that write resolves what the priority
asks. A yes clears it, closes a follow-up, records an operator comment on a question as its answer and notes the
reason on the item; a no or a silent classifier leaves it. A priority that waits on an operator decision, a question,
a merge approval, a follow-up flagged for him or an escalated plan, counts only the operator's writes, a verified
master relay among them; an agent's write on it is never judged.
"""

import re
from dataclasses import dataclass

from hooks.classifier import ClassifierError, decide, runner
from scripts.swarm import ledger_events

PURPOSE = "priority-resolve"
CURSOR = "priority-cursor"
PATH = re.compile(r"([a-z]+)/([^/]+)")
CLEARED = "Priority cleared by the swarm: {reason}."
OPERATOR = "operator"
SELF = ("swarm", "ledger")
WRITES = {
    "comment added": "comment",
    "comment edited": "comment",
    "answer added": "answer",
    "answer edited": "answer",
    "message added": "chat line",
}


@dataclass(frozen=True)
class Write:
    by: str
    kind: str
    target: str
    text: str

    @property
    def who(self):
        return "the operator" if self.by == OPERATOR else "an agent"


def priority_pass(store, slug, doc, ledger, judge=None, github=None):
    judge, github = judge or decide, github or ledger_events.view
    rows = doc.get("priorities", [])
    actions, gone = [], set()
    for row, reason in sweep(doc, rows, github):
        ledger.clear_priority(slug, row["id"], reason)
        actions.append(_cleared(row, reason))
        gone.add(row["id"])
    judged = set()
    for row, write in _writes(doc, rows, ledger_events.new_events(store, slug, doc, CURSOR)):
        if row["id"] in gone or (row["id"], write.text) in judged:
            continue
        judged.add((row["id"], write.text))
        yes = _judge(doc, row, write, judge)
        if yes is not None:
            reason = (
                f"the classifier judged that the {write.kind} from {write.who} resolves it, at probability {yes:.2f}"
            )
            _resolve(ledger, slug, row, write, reason)
            actions.append(_cleared(row, reason))
            gone.add(row["id"])
    return actions


def sweep(doc, rows, github):
    for row in rows:
        if not row.get("derived"):
            reason = _stale(_item(doc, row["item"]), github)
            if reason:
                yield row, reason


def _stale(item, github):
    if not item:
        return "its item is gone"
    if item.get("out_of_scope"):
        return "its item is out of scope"
    if item.get("done") or item.get("state") == "done":
        return "its item is done"
    if item.get("state") == "pr" and item.get("pr_url"):
        found = github(item["pr_url"])
        if found is not None and found.state == "MERGED":
            return "its pull request merged"
    return None


def _item(doc, path):
    name, item_id = _split(path)
    return next((i for i in doc[name] if i["id"] == item_id), {})


def _split(path):
    return PATH.fullmatch(path).groups()


def _writes(doc, rows, events):
    for event in events:
        kind = WRITES.get(event["kind"])
        if kind is None or event["by"] in SELF:
            continue
        write = Write(event["by"], kind, event["target"], _text(doc, event))
        if not write.text.strip():
            continue
        for row in rows:
            if row["at"] <= event["at"] and _about(row, write) and _counts(doc, row, write):
                yield row, write


def _counts(doc, row, write):
    return write.by == OPERATOR or not _operator_decides(row["item"], _item(doc, row["item"]))


def _operator_decides(path, item):
    name = _split(path)[0]
    if name == "questions":
        return True
    if name == "tasks":
        return item.get("awaiting") == "approval"
    if name == "followups":
        return bool(item.get("needs_operator"))
    return bool((item.get("review") or {}).get("escalated"))


def _text(doc, event):
    if "text" in event:
        return event["text"]
    item = _item(doc, event["target"])
    entries = item.get("comments", []) + item.get("answers", [])
    return next((e["text"] for e in entries if e["id"] == event["id"]), "")


def _about(row, write):
    if write.target != "chat":
        return write.target == row["item"]
    item_id = _split(row["item"])[1]
    return re.search(rf"(?<![\w-]){re.escape(item_id)}(?![\w-])", write.text) is not None


def _subject(doc, path):
    item = _item(doc, path)
    return item.get("title") or item.get("text") or path


def _judge(doc, row, write, judge):
    subject = _subject(doc, row["item"])
    state = {
        "item": subject,
        "priority": row["text"],
        "write": {"by": write.who, "kind": write.kind, "text": write.text},
    }
    params = {"kind": write.kind, "subject": subject, "priority": row["text"]}
    try:
        output = runner.run(PURPOSE, state, params, decider=judge)
    except ClassifierError:
        return None
    yes = output.raw.answers["resolves"].noul
    return yes if yes is not None and yes >= output.thresholds["probability"] else None


def _resolve(ledger, slug, row, write, reason):
    ledger.clear_priority(slug, row["id"], reason)
    name = _split(row["item"])[0]
    if name == "followups":
        ledger.mark_done(slug, row["item"])
    if name == "questions" and write.by == OPERATOR and write.kind == "comment":
        ledger.answer_as_operator(slug, row["item"], write.text)
    ledger.comment_item(slug, row["item"], CLEARED.format(reason=reason))


def _cleared(row, reason):
    return f"cleared the priority on {row['item']}: {reason}"
