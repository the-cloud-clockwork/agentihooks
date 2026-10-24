"""Doc item reports: a done task naming watchdog doc items tells the watchdog, and its answer is recorded on the task."""

import hashlib
import os
from datetime import UTC, datetime

from scripts.inbox.store import DuplicateItem, InboxError, InboxStore, replied

ADDRESS_ENV, DEFAULT_ADDRESS = "AGENTIHOOKS_WATCHDOG_ADDRESS", "master-watchdog"
ACK_MINUTES_ENV, DEFAULT_ACK_MINUTES = "AGENTIHOOKS_WATCHDOG_ACK_MINUTES", 30
FIELD, ITEMS = "watchdog", "doc_items"
SENDER = "swarm"
ANSWERED = ("done", "cancelled")
STAMP = "%Y-%m-%dT%H:%M:%SZ"
ASK = "The watchdog has not acknowledged the doc item report for this task. Ask it to read its inbox and reply."


def address() -> str:
    return os.environ.get(ADDRESS_ENV, "").strip() or DEFAULT_ADDRESS


def ack_ms() -> int:
    return int(os.environ.get(ACK_MINUTES_ENV) or DEFAULT_ACK_MINUTES) * 60_000


def utc(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, UTC).strftime(STAMP)


def _ms(stamp: str) -> int:
    return int(datetime.strptime(stamp, STAMP).replace(tzinfo=UTC).timestamp() * 1000)


def report_id(slug: str, task_id: str, pr_url: str) -> str:
    return hashlib.sha256(f"doc-report:{slug}:{task_id}:{pr_url}".encode()).hexdigest()[:12]


def text(slug: str, task: dict, at_ms: int) -> str:
    proof = task.get("proof") or {}
    lines = [
        f"Doc item report from swarm {slug}, task {task['id']}: {task.get('title', '')}",
        "Status: done",
        "Doc items: " + "; ".join(task[ITEMS]),
        f"Merged pull request: {task.get('pr_url') or 'none recorded'}",
        "Proof run: " + ("; ".join(f"{k}: {v}" for k, v in sorted(proof.items())) or "none recorded"),
        f"Done at: {utc(at_ms)}",
        "Reply to this message to acknowledge it; the ledger records your reply on the task.",
    ]
    return "\n".join(lines)


def _current(task: dict) -> bool:
    record = task.get(FIELD) or {}
    return bool(record.get("item")) and record.get("pr") == task["pr_url"]


def _send(inbox: InboxStore, slug: str, task: dict, record: dict) -> None:
    body = text(slug, task, _ms(record["sent_at"]))
    try:
        inbox.send(SENDER, record["to"], body, task=task["id"], item_id=record["item"])
    except DuplicateItem:
        pass


def report(inbox: InboxStore, ledger, slug: str, task: dict, at_ms: int) -> str:
    if task.get("state") != "done" or not task.get(ITEMS) or _current(task):
        return ""
    pr_url = task["pr_url"]
    record = {"item": report_id(slug, task["id"], pr_url), "to": address(), "pr": pr_url, "sent_at": utc(at_ms)}
    ledger.update_task(slug, task["id"], {FIELD: record})
    _send(inbox, slug, task, record)
    return f"reported doc items of {task['id']} to {record['to']}"


def _answer(inbox: InboxStore, item) -> str | None:
    if item.state not in ANSWERED:
        return None
    detail = item.reason.removeprefix(f"{item.state}: ")
    if detail.startswith(replied("")):
        return inbox.get(detail.removeprefix(replied(""))).text
    return detail


def _settle(inbox: InboxStore, ledger, slug: str, task: dict, at_ms: int) -> str:
    record = task[FIELD]
    if record.get("acked_at"):
        return ""
    try:
        item = inbox.get(record["item"])
    except InboxError:
        _send(inbox, slug, task, record)
        return f"resent the doc item report of {task['id']} to {record['to']}"
    answer = _answer(inbox, item)
    if answer is not None:
        ledger.update_task(slug, task["id"], {FIELD: {**record, "acked_at": utc(at_ms), "ack": answer}})
        return f"recorded the watchdog acknowledgment of {task['id']}"
    if record.get("priority") or at_ms - _ms(record["sent_at"]) < ack_ms():
        return ""
    ledger.priority(slug, f"tasks/{task['id']}", ASK)
    ledger.update_task(slug, task["id"], {FIELD: {**record, "priority": utc(at_ms)}})
    return f"raised a Priority for the unacknowledged doc item report of {task['id']}"


def report_pass(inbox: InboxStore, ledger, slug: str, tasks: list[dict], at_ms: int) -> list[str]:
    actions = []
    for task in tasks:
        step = _settle if _current(task) else report
        actions.append(step(inbox, ledger, slug, task, at_ms))
    return [a for a in actions if a]
