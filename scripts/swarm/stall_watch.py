"""Stall watchdog: each tick holds every open swarm pull request and every lane against a limit.

A pull request left green and unqueued, red with no push, without a live owner, or unmergeable reopens its task for
the next engineer, who continues from the stored handoff and branch. A ready task left unclaimed while its lane is full
and accounts show free seats only raises the alert. Every alert reaches the master's inbox and the health findings;
one still open two more limits later reaches the operator's Priorities and ntfy.
"""

import json
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, fields

from scripts.inbox import exits
from scripts.inbox.store import CLOSED, InboxError, InboxStore
from scripts.swarm import capacity, outage, ready
from scripts.swarm.health.findings import MINUTE_MS, Finding
from scripts.swarm.ledger_events import Mail

KIND = "stall"
SENDER = "swarm"
HELD = ("claimed", "pr")
NTFY = "AGENTIHOOKS_STALL_NTFY_URL"
ENV = {
    "green": "AGENTIHOOKS_STALL_GREEN_MINUTES",
    "red": "AGENTIHOOKS_STALL_RED_MINUTES",
    "owner": "AGENTIHOOKS_STALL_OWNER_MINUTES",
    "unmergeable": "AGENTIHOOKS_STALL_UNMERGEABLE_MINUTES",
    "unclaimed": "AGENTIHOOKS_STALL_UNCLAIMED_MINUTES",
}
CASES = {
    "green": "green and not queued",
    "red": "red with no push",
    "owner": "owner not live",
    "unmergeable": "unmergeable or dropped from the queue",
    "unclaimed": "ready and unclaimed while its lane is full",
}
REOPENED = "reopened the task for the next engineer with its handoff and branch"
ALERTED = "alert only; the lane split owns the cause"
ALERT = (
    "Stall on swarm {slug}: task {task} ({title}) is {case} for {minutes} minutes. Pull request: {pr}. Owner: {owner}. "
    "Action: {action}. Answer by closing this item; still open in {wait} minutes, it goes to the operator."
)
COMMENT = "The stall watchdog {action}: its pull request {pr} was {case} for {minutes} minutes, past the limit."
OPERATOR = "Stall unanswered: task {task} {case} for {minutes} minutes, pull request {pr}, owner {owner}, {action}."


@dataclass(frozen=True)
class Limits:
    green: int = 10
    red: int = 15
    owner: int = 10
    unmergeable: int = 5
    unclaimed: int = 15


def limits(environ=None):
    env = os.environ if environ is None else environ
    values = {}
    for field in fields(Limits):
        raw = env.get(ENV[field.name], "")
        if raw.isdigit() and int(raw) > 0:
            values[field.name] = int(raw)
    return Limits(**values)


def ntfy(url, title, text, opener=urllib.request.urlopen):
    """An empty answer when posted, else why not."""
    request = urllib.request.Request(url, data=text.encode(), headers={"Title": title}, method="POST")
    try:
        with opener(request, timeout=10):
            return ""
    except (OSError, ValueError) as exc:
        return f"ntfy failed: {exc}"


@dataclass(frozen=True)
class Watch:
    store: object
    slug: str
    ledger: object
    view: Callable
    live: frozenset
    retire: Callable
    claimable: Callable
    now_ms: int
    environ: dict | None = None
    post: Callable = ntfy


def _key(watch, name):
    return watch.store.key(watch.slug, name)


def stall_pass(watch, doc):
    limit = limits(watch.environ)
    mail = Mail(InboxStore(watch.store.redis), watch.store, watch.slug, doc)
    rows = {t["id"]: t for t in doc.get("tasks", [])}
    marks, seen, actions = watch.store.redis.hgetall(_key(watch, "stall-seen")), {}, []
    held = [task for task in rows.values() if task.get("state") in HELD and task.get("pr_url")]
    gone = watch.store.redis.smembers(_key(watch, "stall-queued")) - {task["id"] for task in held}
    if gone:
        watch.store.redis.srem(_key(watch, "stall-queued"), *gone)
    for task in held:
        actions += _pull_request(watch, mail, task, limit, marks, seen)
    actions += _unclaimed(watch, mail, rows, limit, marks, seen)
    watch.store.redis.delete(_key(watch, "stall-seen"))
    if seen:
        watch.store.redis.hset(_key(watch, "stall-seen"), mapping=seen)
    return actions + _escalate(watch, rows)


def _since(watch, marks, seen, mark):
    seen[mark] = int(marks.get(mark, watch.now_ms))
    return seen[mark]


def _stall(watch, task, pull, marks, seen):
    tid, queued = task["id"], _key(watch, "stall-queued")
    if pull.queued:
        watch.store.redis.sadd(queued, tid)
    if pull.mergeable == "CONFLICTING" or (not pull.queued and watch.store.redis.sismember(queued, tid)):
        return "unmergeable", _since(watch, marks, seen, f"{tid}:unmergeable")
    marks_red = [mark for mark in (pull.pushed_at, pull.red_at) if mark is not None]
    if pull.red and pull.resolved and marks_red:
        return "red", max(marks_red)
    if pull.resolved and not pull.queued and not pull.draft:
        return "green", _since(watch, marks, seen, f"{tid}:green")
    if not ready.awaiting(task) and task.get("claimed_by") not in watch.live:
        return "owner", _since(watch, marks, seen, f"{tid}:owner")
    return None, None


def _pull_request(watch, mail, task, limit, marks, seen):
    pull = watch.view(task["pr_url"])
    if pull is None:
        return []
    if pull.state != "OPEN":
        watch.store.redis.srem(_key(watch, "stall-queued"), task["id"])
        return []
    case, since = _stall(watch, task, pull, marks, seen)
    minutes = (watch.now_ms - since) // MINUTE_MS if case else 0
    if not case or minutes < getattr(limit, case):
        return []
    return _reopen(watch, mail, task, case, minutes, limit)


def _reopen(watch, mail, task, case, minutes, limit):
    task = dict(task)
    tid, owner = task["id"], task.get("claimed_by") or ""
    fresh = watch.ledger.task(watch.slug, tid) or {}
    if fresh.get("state") not in HELD or (fresh.get("claimed_by") or "") != owner:
        return []
    if outage.held(watch.store, watch.slug, owner or None):
        return []
    agent = next((a for a in watch.store.agents(watch.slug) if a.name == owner and a.state != "finished"), None)
    if agent is not None and agent.name in watch.live and not watch.retire(agent):
        return [f"could not retire {owner} for stalled task {tid}, retrying next tick"]
    reopened = watch.ledger.update_task(
        watch.slug, tid, {"state": "open", "claimed_by": "", "awaiting": ""}, if_state=HELD
    )
    if reopened.get("state") != "open":
        return []
    if owner:
        watch.store.release(watch.slug, tid, owner)
    if agent is not None:
        watch.store.drop_agent(watch.slug, owner)
        exits.settle(InboxStore(watch.store.redis), owner, agent.seat, "stopped")
    ready.forget(watch.store, watch.slug, tid)
    watch.store.redis.srem(_key(watch, "stall-queued"), tid)
    text = COMMENT.format(action=REOPENED, pr=task["pr_url"], case=CASES[case], minutes=minutes)
    watch.ledger.comment(watch.slug, tid, text, by=SENDER)
    return _alert(watch, mail, task, case, minutes, REOPENED, limit)


def _alert(watch, mail, task, case, minutes, action, limit):
    tid, wait = task["id"], 2 * getattr(limit, case)
    row = {
        "task": tid,
        "case": case,
        "pr": task.get("pr_url") or "none",
        "owner": task.get("claimed_by") or "nobody",
        "minutes": minutes,
        "action": action,
        "limit": getattr(limit, case),
        "at": watch.now_ms,
    }
    address, inbox = mail.owner(f"tasks/{tid}"), mail.inbox
    earlier = watch.store.redis.hget(_key(watch, "stall-alerts"), tid)
    if earlier and _open(inbox, json.loads(earlier)["item"]):
        inbox.close(json.loads(earlier)["item"], SENDER, "done", f"superseded by a new stall alert on task {tid}")
    text = ALERT.format(
        slug=watch.slug, title=task.get("title") or "no title", wait=wait, **{**row, "case": CASES[case]}
    )
    row["item"] = mail.inbox.send(SENDER, address, text, ref=f"{watch.slug}:stall:{tid}").id
    watch.store.redis.hset(_key(watch, "stall-alerts"), tid, json.dumps(row))
    return [f"stall: task {tid} {CASES[case]} for {minutes} minutes, {action}; told {address}"]


def _lane_full(watch, lane, decision):
    configured = (decision.get("configured") or {}).get(lane, 0)
    working = [a for a in watch.store.agents(watch.slug) if a.lane == lane and a.state != "finished"]
    return configured > 0 and len(working) >= configured


def _unclaimed(watch, mail, rows, limit, marks, seen):
    decision, key = capacity.read(watch.store, watch.slug), _key(watch, "stall-alerted")
    alerted, current, actions = watch.store.redis.hgetall(key), {}, []
    if sum((decision.get("placeable") or {}).values()) > 0:
        for lane in ("eng", "ci"):
            if not _lane_full(watch, lane, decision):
                continue
            for task in watch.claimable(lane):
                since = current[task["id"]] = str(_since(watch, marks, seen, f"{task['id']}:unclaimed"))
                minutes = (watch.now_ms - int(since)) // MINUTE_MS
                if minutes >= limit.unclaimed and alerted.get(task["id"]) != since:
                    actions += _alert(watch, mail, rows.get(task["id"], task), "unclaimed", minutes, ALERTED, limit)
                    alerted[task["id"]] = since
    watch.store.redis.delete(key)
    kept = {tid: since for tid, since in alerted.items() if current.get(tid) == since}
    if kept:
        watch.store.redis.hset(key, mapping=kept)
    return actions


def _open(inbox, item_id):
    try:
        return inbox.get(item_id).state not in CLOSED
    except InboxError:
        return False


def _escalate(watch, rows):
    inbox, key, actions = InboxStore(watch.store.redis), _key(watch, "stall-alerts"), []
    for tid, raw in watch.store.redis.hgetall(key).items():
        row = json.loads(raw)
        task = rows.get(tid, {})
        moot = row["case"] == "unclaimed" and (task.get("state") != "open" or task.get("claimed_by"))
        if moot and _open(inbox, row["item"]):
            inbox.close(row["item"], SENDER, "done", f"task {tid} was claimed")
        if moot or not _open(inbox, row["item"]):
            watch.store.redis.hdel(key, tid)
            continue
        if row.get("escalated") or watch.now_ms - row["at"] < 2 * row["limit"] * MINUTE_MS:
            continue
        text = OPERATOR.format(**{**row, "case": CASES[row["case"]]})
        watch.ledger.priority(watch.slug, f"tasks/{tid}", text)
        url = (os.environ if watch.environ is None else watch.environ).get(NTFY, "")
        refused = watch.post(url, f"Swarm {watch.slug} stall", text) if url else "ntfy is not configured"
        row["escalated"] = watch.now_ms
        watch.store.redis.hset(key, tid, json.dumps(row))
        actions.append(f"stall on task {tid} unanswered, raised to the operator" + (f"; {refused}" if refused else ""))
    return actions


def findings(store, slug):
    found = []
    for tid, raw in sorted(store.redis.hgetall(store.key(slug, "stall-alerts")).items()):
        row = json.loads(raw)
        found.append(
            Finding(
                KIND,
                tid,
                f"{CASES[row['case']]} for {row['minutes']} minutes",
                (f"pull request {row['pr']}", f"owner {row['owner']}", f"action: {row['action']}"),
                f"{row['limit']} minutes {CASES[row['case']]}",
                row["minutes"],
            )
        )
    return found
