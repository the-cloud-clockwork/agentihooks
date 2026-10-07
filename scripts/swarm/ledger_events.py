"""Agent writes on a swarm ledger as inbox items, sent by the minute tick so the master hears of them asleep or awake.

New ledger events past the swarm's cursor go to the master. Time rules raise a follow-up nobody decided, watch each
task's pull request on GitHub and pass every new health finding on for a verdict. Each item is sent once, so a replay
of the same ledger sends nothing; the wake ladder then carries every item to a reader.
"""

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime

from scripts.inbox.seats import seat_address
from scripts.swarm.health.verdicts import VERDICTS
from scripts.swarm.naming import lane_of
from scripts.swarm.store import MASTER

MINUTE_MS = 60_000
FOLLOWUP_RAISE_MS = 15 * MINUTE_MS
FOLLOWUP_OPERATOR_MS = 30 * MINUTE_MS
MERGED_ENGINEER_MS = 10 * MINUTE_MS
MERGED_MASTER_MS = 20 * MINUTE_MS
RED_QUIET_MS = 20 * MINUTE_MS
SENT_TTL_S = 30 * 24 * 3600
SENDER = "swarm"
OPERATOR = "operator"
ASK_WORDS = 12
RED = {"FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}
PASSED = {"SUCCESS", "SKIPPED"}
FIELDS = "state,mergedAt,commits,statusCheckRollup,headRefOid"


@dataclass(frozen=True)
class PullRequest:
    state: str
    merged_at: int | None
    pushed_at: int | None
    red: bool
    resolved: bool = False
    failed: tuple = ()
    head: str = ""


def iso_ms(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def pull_request(raw):
    commits, checks = raw.get("commits") or [], raw.get("statusCheckRollup") or []
    results = [check.get("conclusion") or check.get("state") for check in checks]
    return PullRequest(
        raw["state"],
        iso_ms(raw["mergedAt"]) if raw.get("mergedAt") else None,
        iso_ms(commits[-1]["committedDate"]) if commits else None,
        any(result in RED for result in results),
        any(result in RED - {"TIMED_OUT"} for result in results)
        or (bool(results) and all(result in PASSED for result in results)),
        tuple(
            check.get("name") or check.get("context") or "a check"
            for check, result in zip(checks, results)
            if result in RED
        ),
        raw.get("headRefOid") or "",
    )


def view(url, run=subprocess.run):
    try:
        done = run(["gh", "pr", "view", url, "--json", FIELDS], capture_output=True, text=True, timeout=20)
        return pull_request(json.loads(done.stdout)) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


class Mail:
    def __init__(self, inbox, store, slug):
        self.inbox, self.store, self.slug = inbox, store, slug
        live = [a for a in store.agents(slug) if a.state != "finished"]
        self.seats = {a.name: a.seat or a.name for a in live}
        boss = next((a for a in live if a.lane == MASTER), None)
        self.master = self.seats[boss.name] if boss else seat_address(slug, MASTER)

    def once(self, key, act):
        marker = self.store.key(self.slug, "events-sent", key)
        if self.store.redis.exists(marker):
            return False
        act()
        self.store.redis.set(marker, 1, ex=SENT_TTL_S)
        return True

    def send(self, key, address, text, ref="", fyi=False):
        if self.once(key, lambda: self.inbox.send(SENDER, address, text, ref=ref, fyi=fyi)):
            return [f"told {address}: {key}"]
        return []

    def engineer(self, task):
        return self.seats.get(task.get("claimed_by", ""), self.master)


def event_pass(inbox, store, slug, doc, ledger, now_ms, github=view):
    mail = Mail(inbox, store, slug)
    events = doc.get("_meta", {}).get("events", [])
    tasks = {t["id"]: t for t in doc.get("tasks", [])}
    return (
        _events(mail, new_events(store, slug, doc, "events-cursor"), tasks)
        + _followups(mail, doc, events, ledger, now_ms)
        + _pull_requests(mail, tasks.values(), now_ms, github)
    )


def findings_pass(inbox, store, slug, shown):
    mail, sent = Mail(inbox, store, slug), []
    for found in shown:
        judged = (found.get("verdict") or {}).get("at", 0)
        text = (
            f"New health finding on swarm {slug}: {found['summary']} ({found['kind']}). Give it a verdict: "
            f'agentihooks swarm {slug} verdict {found["id"]} {"|".join(VERDICTS)} --note "<why>"'
        )
        sent += mail.send(f"finding:{found['id']}:{judged}", mail.master, text)
    return sent


def new_events(store, slug, doc, cursor_name):
    key, rev = store.key(slug, cursor_name), doc["_meta"]["rev"]
    cursor = store.redis.get(key)
    store.redis.set(key, rev)
    if cursor is None:
        return []
    return [e for e in doc["_meta"].get("events", []) if int(cursor) < e.get("rev", 0) <= rev]


def _by_agent(mail, event):
    by = event.get("by", "")
    if by == OPERATOR:
        return False
    return not (lane_of(by) == MASTER and mail.store.names.slug_of(by) == mail.slug)


def _events(mail, events, tasks):
    sent = []
    for event in filter(lambda e: _by_agent(mail, e), events):
        text = _describe(mail.slug, event, tasks)
        if text:
            sent += mail.send(
                f"event:{event['rev']}:{event['kind']}:{event['target']}",
                mail.master,
                text,
                ref=f"{mail.slug}:event:{event['target']}",
                fyi=event["kind"] == "task done",
            )
    return sent


def _describe(slug, event, tasks):
    kind, target, by = event["kind"], event.get("target", ""), event["by"]
    name, _, item = target.partition("/")
    if kind == "added" and name == "followups":
        return (
            f"{by} added a follow-up on ledger {slug}: {event.get('text', '')}\n"
            "Decide it: turn it into a task, close it with a status, or flag it for the operator."
        )
    if kind == "added" and name == "questions":
        return f"{by} asked on ledger {slug}: {event.get('text', '')}\nAnswer it, or raise it to the operator."
    task = tasks.get(item, {})
    title = f"task {item} ({task.get('title', 'no title')})"
    if kind == "task blocked":
        return f"{by} blocked {title} on ledger {slug}. Read its last comment, then unblock, rewrite or raise it."
    if kind == "task done":
        proof = " ".join(filter(None, (task.get("pr_url", ""), json.dumps(task["proof"]) if task.get("proof") else "")))
        return f"{by} closed {title} as done on ledger {slug}. Check its proof: {proof or 'none recorded'}."
    return ""


def _followups(mail, doc, events, ledger, now_ms):
    added = {
        e["target"]: e for e in events if e.get("kind") == "added" and e.get("target", "").startswith("followups/")
    }
    sent = []
    for followup in doc.get("followups", []):
        target = f"followups/{followup['id']}"
        event = added.get(target)
        if followup.get("done") or followup.get("needs_operator") or not event or event["by"] == OPERATOR:
            continue
        age = now_ms - event["at"]
        if age >= FOLLOWUP_RAISE_MS:
            text = (
                f"A follow-up on ledger {mail.slug} is still open fifteen minutes after {event['by']} added it: "
                f"{followup['text']}\nDecide it now, or it goes to the operator's Priorities."
            )
            sent += mail.send(f"{target}:raised", mail.master, text, ref=f"{mail.slug}:event:{target}")
        if age >= FOLLOWUP_OPERATOR_MS:
            ask = "Open half an hour without a decision: " + " ".join(followup["text"].split()[:ASK_WORDS])
            if mail.once(f"{target}:operator", lambda: ledger.priority(mail.slug, target, ask)):
                sent.append(f"raised {target} to the operator")
    return sent


def _pull_requests(mail, tasks, now_ms, github):
    sent = []
    for task in tasks:
        url = task.get("pr_url", "")
        if task.get("state") != "pr" or not url:
            continue
        found = github(url)
        if found is None:
            continue
        title = f"task {task['id']} ({task.get('title', 'no title')})"
        if found.state == "MERGED" and found.merged_at:
            age = now_ms - found.merged_at
            if age >= MERGED_ENGINEER_MS:
                text = (
                    f"Your pull request {url} merged ten minutes ago and {title} is still in pull request. "
                    f"Finish it: agentihooks swarm {mail.slug} done --pr {url}"
                )
                sent += mail.send(f"{url}:merged:engineer", mail.engineer(task), text)
            if age >= MERGED_MASTER_MS:
                text = f"{title} on ledger {mail.slug} is still in pull request twenty minutes after {url} merged."
                sent += mail.send(f"{url}:merged:master", mail.master, text)
        elif found.state == "CLOSED":
            text = f"Your pull request {url} for {title} was closed without merging. Reopen it, open a new one, or block the task."
            sent += mail.send(f"{url}:closed", mail.engineer(task), text)
        elif found.red and found.pushed_at and now_ms - found.pushed_at >= RED_QUIET_MS:
            text = (
                f"Your pull request {url} for {title} has red checks and no push for twenty minutes. Fix them and push."
            )
            sent += mail.send(f"{url}:red:{found.pushed_at}", mail.engineer(task), text)
    return sent
