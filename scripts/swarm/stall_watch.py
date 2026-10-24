"""Stall watchdog: each tick holds every open swarm pull request and every lane against a limit.

In enforce mode, a pull request left green and unqueued, red with no push, without a live owner, or unmergeable reopens its task for
the next engineer, who continues from the stored handoff and branch. A ready task left unclaimed while its lane is full
and accounts show free seats only raises the alert. Every alert reaches the master's inbox and the health findings;
one still open two more limits later reaches the operator's Priorities and ntfy.
"""

import base64
import json
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, fields

from scripts.gates.base import Who
from scripts.gates.enforcement import STALL_WATCH
from scripts.inbox import exits
from scripts.inbox.store import CLOSED, InboxError, InboxStore
from scripts.swarm import capacity, idle, outage, ready, waits
from scripts.swarm.health.findings import MINUTE_MS, Finding
from scripts.swarm.ledger_events import Mail

KIND = "stall"
SENDER = "swarm"
HELD = ("claimed", "pr")
NTFY = "AGENTIHOOKS_STALL_NTFY_URL"
NTFY_USER, NTFY_PASS = "NTFY_USER", "NTFY_PASS"
ANTON_ALERTS = "https://ntfy.homeofanton.com/anton-alerts"
TITLE, AUTHORIZATION, TIMEOUT_S = "Title", "Authorization", 10
QUEUED, SEEN, ALERTS, ALERTED_KEY = "stall-queued", "stall-seen", "stall-alerts", "stall-alerted"
LANES = ("eng", "ci")
FINISHED, STOPPED, DONE, UNCLAIMED = "finished", "stopped", "done", "unclaimed"
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
        raw = env.get(ENV[field.name])
        if raw and raw.isdigit() and int(raw) > 0:
            values[field.name] = int(raw)
    return Limits(**values)


def ntfy(url, title, text, environ, opener=urllib.request.urlopen):
    """An empty answer when posted, else why not."""
    headers = {TITLE: title}
    user, password = environ.get(NTFY_USER), environ.get(NTFY_PASS)
    if user and password:
        headers[AUTHORIZATION] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    request = urllib.request.Request(url, data=text.encode(), headers=headers)
    try:
        with opener(request, timeout=TIMEOUT_S):
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
    released: Callable = lambda task: True
    reread: Callable | None = None


def _key(watch, name):
    return watch.store.key(watch.slug, name)


def stall_pass(watch, doc):
    limit = limits(watch.environ)
    mail = Mail(InboxStore(watch.store.redis), watch.store, watch.slug, doc)
    rows = {t["id"]: t for t in doc.get("tasks") or []}
    marks, seen, actions = watch.store.redis.hgetall(_key(watch, SEEN)), {}, []
    held = [task for task in rows.values() if task.get("state") in HELD and task.get("pr_url")]
    gone = watch.store.redis.smembers(_key(watch, QUEUED)) - {task["id"] for task in held}
    if gone:
        watch.store.redis.srem(_key(watch, QUEUED), *gone)
    for task in held:
        if _waiting(watch, task, rows):
            continue
        actions += _pull_request(watch, mail, task, limit, marks, seen)
    actions += _unclaimed(watch, mail, rows, limit, marks, seen)
    watch.store.redis.delete(_key(watch, SEEN))
    if seen:
        watch.store.redis.hset(_key(watch, SEEN), mapping=seen)
    return actions + _escalate(watch, rows)


def _waiting(watch, task, rows):
    tid, owner = task["id"], task.get("claimed_by")
    if not watch.released(task):
        return True
    if any(dep != tid and rows.get(dep, {}).get("state") != "done" for dep in task.get("depends_on") or []):
        return True
    found = ready.record(watch.store, watch.slug, tid) if ready.awaiting(task) else None
    if (found or {}).get("on", {}).get("kind") == ready.RECOVER:
        return True
    declared = idle.wait(watch.store.redis, watch.slug, owner) if owner else None
    held = ((found or {}).get("dependency"), (declared or {}).get("on"))
    return any(on and waits.dependency(on, task, rows, watch.view) for on in held)


def _minutes(watch, since):
    return (watch.now_ms - since - outage.paused_ms(watch.slug, since, watch.now_ms)) // MINUTE_MS


def _since(watch, marks, seen, mark):
    seen[mark] = int(marks.get(mark, watch.now_ms))
    return seen[mark]


def _woken(watch, task):
    owner = task.get("claimed_by")
    return idle.woke(watch.store.redis, watch.slug, owner) if owner else 0


def _stall(watch, task, pull, marks, seen, agent):
    tid, queued = task["id"], _key(watch, QUEUED)
    if pull.queued:
        watch.store.redis.sadd(queued, tid)
    dropped = watch.store.redis.sismember(queued, tid) and not pull.required_pending
    if not pull.queued and (pull.mergeable == "CONFLICTING" or dropped):
        if pull.draft or (agent is not None and agent.name in watch.live and agent.state == "working"):
            return None, None
        return "unmergeable", _since(watch, marks, seen, f"{tid}:unmergeable")
    marks_red = [mark for mark in (pull.pushed_at, pull.red_at) if mark is not None]
    if (
        pull.red
        and pull.resolved
        and not pull.queued
        and marks_red
        and not pull.latest_run_cancelled
        and not pull.stale_red
        and not pull.draft
    ):
        return "red", max(marks_red)
    if ready.awaiting(task):
        return None, None
    if pull.resolved and not pull.red and not pull.queued and not pull.draft:
        return "green", _since(watch, marks, seen, f"{tid}:green")
    if task.get("claimed_by") not in watch.live:
        return "owner", _since(watch, marks, seen, f"{tid}:owner")
    return None, None


def _pull_request(watch, mail, task, limit, marks, seen):
    pull = watch.view(task["pr_url"])
    if pull is None:
        return []
    if pull.state != "OPEN":
        watch.store.redis.srem(_key(watch, QUEUED), task["id"])
        return []
    agent = next(
        (a for a in watch.store.agents(watch.slug) if a.name == task.get("claimed_by") and a.task == task["id"]),
        None,
    )
    case, minutes = _judged(watch, task, pull, marks, seen, agent)
    if case is None or minutes < getattr(limit, case):
        return []
    fresh = (watch.reread or watch.view)(task["pr_url"])
    if fresh is None or fresh.state != "OPEN":
        return []
    case, minutes = _judged(watch, task, fresh, marks, seen, agent)
    if case is None or minutes < getattr(limit, case):
        return []
    return _reopen(watch, mail, task, case, minutes, limit)


def _judged(watch, task, pull, marks, seen, agent):
    case, since = _stall(watch, task, pull, marks, seen, agent)
    if case is None:
        return None, 0
    if case != "owner":
        since = max(since, pull.pushed_at or 0, pull.red_at or 0, agent.started_at if agent else 0, _woken(watch, task))
    return case, _minutes(watch, since)


def _reopen(watch, mail, task, case, minutes, limit):
    task = dict(task)
    tid, owner = task["id"], task.get("claimed_by") or ""
    fresh = watch.ledger.task(watch.slug, tid) or {}
    if fresh.get("state") not in HELD or (fresh.get("claimed_by") or "") != owner:
        return []
    if case in {"green", "owner"} and ready.awaiting(fresh):
        return []
    if outage.held(watch.store, watch.slug, owner or None):
        return []
    agent = next((a for a in watch.store.agents(watch.slug) if a.name == owner and a.state != FINISHED), None)
    if not _may_reopen(watch, task, agent, case, minutes):
        return _alert(watch, mail, task, case, minutes, ALERTED, limit)
    if agent is not None and agent.name in watch.live and not watch.retire(agent):
        return [f"could not retire {owner} for stalled task {tid}, retrying next tick"]
    reopened = watch.ledger.update_task(
        watch.slug, tid, {"state": "open", "claimed_by": "", "awaiting": ""}, if_state=HELD
    )
    if reopened.get("state") != "open" or reopened.get("claimed_by"):
        return []
    watch.store.mark_restart(watch.slug, tid, "watchdog")
    if owner:
        watch.store.release(watch.slug, tid, owner)
    if agent is not None:
        watch.store.drop_agent(watch.slug, owner)
        exits.settle(InboxStore(watch.store.redis), owner, agent.seat, STOPPED)
    ready.forget(watch.store, watch.slug, tid)
    watch.store.redis.srem(_key(watch, QUEUED), tid)
    text = COMMENT.format(action=REOPENED, pr=task["pr_url"], case=CASES[case], minutes=minutes)
    watch.ledger.comment(watch.slug, tid, text, by=SENDER)
    return _alert(watch, mail, task, case, minutes, REOPENED, limit)


def _may_reopen(watch, task, agent, case, minutes):
    who = Who(name=task.get("claimed_by") or "", swarm=watch.slug, task=task["id"])
    reason = f"{CASES[case]} for {minutes} minutes"
    allowed = STALL_WATCH.allows(watch.store, who, "reopen", reason, watch.now_ms)
    if agent is not None and agent.name in watch.live:
        allowed = STALL_WATCH.allows(watch.store, who, "stop", reason, watch.now_ms) and allowed
    return allowed


def _earlier(inbox, raw):
    if not raw:
        return []
    item = json.loads(raw)["item"]
    return [item] if _open(inbox, item) else []


def _alert(watch, mail, task, case, minutes, action, limit):
    tid, wait, inbox = task["id"], 2 * getattr(limit, case), mail.inbox
    earlier = _earlier(inbox, watch.store.redis.hget(_key(watch, ALERTS), tid))
    if earlier and action == ALERTED:
        return []
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
    if case == UNCLAIMED:
        row["lane"] = task.get("lane", "")
    address = mail.owner(f"tasks/{tid}")
    text = ALERT.format(
        slug=watch.slug, title=task.get("title") or "no title", wait=wait, **{**row, "case": CASES[case]}
    )
    row["item"] = inbox.send(SENDER, address, text, ref=f"{watch.slug}:stall:{tid}").id
    watch.store.redis.hset(_key(watch, ALERTS), tid, json.dumps(row))
    for item in earlier:
        inbox.close(item, SENDER, DONE, f"superseded by a new stall alert on task {tid}")
    return [f"stall: task {tid} {CASES[case]} for {minutes} minutes, {action}; told {address}"]


def _lane_full(watch, lane, decision):
    if watch.store.redis.hget(watch.store.key(watch.slug, "config"), "scaling") == "manual":
        return False
    configured = (decision.get("configured") or {}).get(lane, 0)
    working = [a for a in watch.store.agents(watch.slug) if a.lane == lane and a.state != FINISHED]
    return configured > 0 and len(working) >= configured


def _lane_tasks(watch, rows, lane):
    tasks = [rows.get(task["id"], task) for task in watch.claimable(lane)]
    return [task for task in tasks if task.get("state", "open") == "open" and not task.get("claimed_by")]


def _unclaimed(watch, mail, rows, limit, marks, seen):
    decision, key = capacity.read(watch.store, watch.slug), _key(watch, ALERTED_KEY)
    alerted, current, actions = watch.store.redis.hgetall(key), {}, []
    if sum((decision.get("placeable") or {}).values()) > 0:
        for lane in LANES:
            tasks = _lane_tasks(watch, rows, lane)
            if not tasks or not _lane_full(watch, lane, decision):
                continue
            since = current[lane] = str(_since(watch, marks, seen, f"{lane}:{UNCLAIMED}"))
            minutes = _minutes(watch, int(since))
            if minutes >= limit.unclaimed and alerted.get(lane) != since:
                task = {**tasks[0], "lane": lane}
                task["title"] = f"{task.get('title') or 'no title'}; {lane} lane: {len(tasks)} ready tasks"
                actions += _alert(watch, mail, task, UNCLAIMED, minutes, ALERTED, limit)
                alerted[lane] = since
    watch.store.redis.delete(key)
    kept = {lane: since for lane, since in alerted.items() if current.get(lane) == since}
    if kept:
        watch.store.redis.hset(key, mapping=kept)
    return actions


def _open(inbox, item_id):
    try:
        return inbox.get(item_id).state not in CLOSED
    except InboxError:
        return False


def _unclaimed_moot(watch, rows, row, task, decision):
    lane = row.get("lane") or task.get("lane")
    if not _lane_full(watch, lane, decision):
        return f"the {lane} lane is no longer full"
    waiting = task.get("state", "open") == "open" and not task.get("claimed_by")
    if not _lane_tasks(watch, rows, lane) and not waiting:
        return f"task {row['task']} was claimed"
    return ""


def _escalate(watch, rows):
    inbox, key, actions = InboxStore(watch.store.redis), _key(watch, ALERTS), []
    decision = capacity.read(watch.store, watch.slug)
    for tid, raw in watch.store.redis.hgetall(key).items():
        row = json.loads(raw)
        task = rows.get(tid, {})
        moot = _unclaimed_moot(watch, rows, row, task, decision) if row["case"] == UNCLAIMED else ""
        is_open = _open(inbox, row["item"])
        if moot and is_open:
            inbox.close(row["item"], SENDER, DONE, moot)
        if moot or not is_open:
            watch.store.redis.hdel(key, tid)
            continue
        if row.get("escalated") or _minutes(watch, row["at"]) < 2 * row["limit"]:
            continue
        text = OPERATOR.format(**{**row, "case": CASES[row["case"]]})
        watch.ledger.priority(watch.slug, f"tasks/{tid}", text)
        env = os.environ if watch.environ is None else watch.environ
        refused = watch.post(env.get(NTFY) or ANTON_ALERTS, f"Swarm {watch.slug} stall", text, env)
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
