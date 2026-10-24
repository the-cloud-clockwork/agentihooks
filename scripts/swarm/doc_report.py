"""Doc item reports: a done task that names watchdog doc items tells the watchdog, and its reply is recorded on the task.

The report goes out once, under an id derived from the swarm and the task, so swarm done and the tick pass that
catches tasks closed by ready never send it twice. The watchdog acknowledges by replying to it; a report left
unacknowledged past the window raises a Priority on the task once.
"""

import hashlib
import os
from datetime import UTC, datetime

from scripts.inbox.store import DuplicateItem, InboxError, InboxStore, replied

ADDRESS_ENV, DEFAULT_ADDRESS = "AGENTIHOOKS_WATCHDOG_ADDRESS", "master-watchdog"
ACK_MINUTES_ENV, DEFAULT_ACK_MINUTES = "AGENTIHOOKS_WATCHDOG_ACK_MINUTES", 30
FIELD, ITEMS = "watchdog", "doc_items"
SENDER = "swarm"
ASK = "The watchdog has not acknowledged the doc item report for this task. Ask it to read its inbox and reply."


def address(environ=None) -> str:
    return (environ or os.environ).get(ADDRESS_ENV, "").strip() or DEFAULT_ADDRESS


def ack_ms(environ=None) -> int:
    raw = (environ or os.environ).get(ACK_MINUTES_ENV, "")
    return (int(raw) if raw.isdigit() else DEFAULT_ACK_MINUTES) * 60_000


def utc(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def report_id(slug: str, task_id: str) -> str:
    return hashlib.sha256(f"doc-report:{slug}:{task_id}".encode()).hexdigest()[:12]


def text(slug: str, task: dict, at_ms: int) -> str:
    proof = task.get("proof") or {}
    lines = [
        f"Doc item report from swarm {slug}, task {task['id']}: {task.get('title', '')}",
        "Doc items: " + "; ".join(task[ITEMS]),
        f"Merged pull request: {task.get('pr_url') or 'none recorded'}",
        "Proof run: " + ("; ".join(f"{k}: {v}" for k, v in sorted(proof.items())) or "none recorded"),
        f"Done at: {utc(at_ms)}",
        "Reply to this message to acknowledge it; the ledger records your reply on the task.",
    ]
    return "\n".join(lines)


def report(inbox: InboxStore, ledger, slug: str, task: dict, at_ms: int, by: str = SENDER) -> str:
    if not task.get(ITEMS) or (task.get(FIELD) or {}).get("item"):
        return ""
    item_id = report_id(slug, task["id"])
    try:
        inbox.send(SENDER, address(), text(slug, task, at_ms), task=task["id"], item_id=item_id)
    except DuplicateItem:
        pass
    ledger.update_task(slug, task["id"], {FIELD: {"item": item_id, "to": address(), "sent_at": utc(at_ms)}}, by=by)
    return f"reported doc items of {task['id']} to {address()}"


def _ack(inbox: InboxStore, item_id: str) -> str | None:
    try:
        item = inbox.get(item_id)
    except InboxError:
        return None
    if item.state != "done":
        return None
    detail = item.reason.removeprefix("done: ")
    if not detail.startswith(replied("")):
        return detail
    try:
        return inbox.get(detail.removeprefix(replied(""))).text
    except InboxError:
        return detail


def _settle(inbox: InboxStore, ledger, slug: str, task: dict, at_ms: int) -> str:
    record = task[FIELD]
    if record.get("acked_at"):
        return ""
    answer = _ack(inbox, record["item"])
    if answer is not None:
        ledger.update_task(slug, task["id"], {FIELD: {**record, "acked_at": utc(at_ms), "ack": answer}})
        return f"recorded the watchdog acknowledgment of {task['id']}"
    sent = datetime.strptime(record["sent_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp() * 1000
    if record.get("priority") or at_ms - sent < ack_ms():
        return ""
    ledger.priority(slug, f"tasks/{task['id']}", ASK)
    ledger.update_task(slug, task["id"], {FIELD: {**record, "priority": utc(at_ms)}})
    return f"raised a Priority for the unacknowledged doc item report of {task['id']}"


def report_pass(inbox: InboxStore, ledger, slug: str, tasks: list[dict], at_ms: int) -> list[str]:
    actions = []
    for task in tasks:
        if task.get("state") != "done" or not task.get(ITEMS):
            continue
        if not (task.get(FIELD) or {}).get("item"):
            actions.append(report(inbox, ledger, slug, task, at_ms))
        else:
            actions.append(_settle(inbox, ledger, slug, task, at_ms))
    return [a for a in actions if a]
