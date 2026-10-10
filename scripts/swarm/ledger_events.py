"""Agent writes on a swarm ledger as inbox items, sent by the minute tick so the master hears of them asleep or awake.

New ledger events past the swarm's cursor go to the master owning their phase, else the lead. Time rules raise a follow-up nobody decided, watch each
task's pull request on GitHub and pass every new health finding on for a verdict. Each item is sent once, so a replay
of the same ledger sends nothing; the wake ladder then carries every item to a reader.
"""

import functools
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scripts.inbox.store import CLOSED, InboxError
from scripts.swarm import operator_mail
from scripts.swarm.health.verdicts import VERDICTS
from scripts.swarm.naming import lane_of
from scripts.swarm.store import MASTER, PREFIX
from scripts.swarm_v2 import masters

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
FINAL_RED = RED - {"TIMED_OUT"}
PASSED = {"SUCCESS", "SKIPPED"}
PENDING = {None, "", "PENDING", "EXPECTED"}
GATE = "Gate — Required"
RED_NOTICE = re.compile(r"^Your pull request (\S+) for .* has red checks and no push for twenty minutes\.")
GATE_JOB = re.compile(rf"^[ \t]+name:[ \t]*(['\"]?){re.escape(GATE)}\1[ \t]*$", re.MULTILINE)
PULL_QUERY = (
    "query($url:URI!){resource(url:$url){...on PullRequest{state mergedAt headRefOid mergeQueueEntry{id} "
    "commits(last:1){nodes{commit{committedDate "
    'file(path:".github/workflows"){object{...on Tree{entries{object{...on Blob{text}}}}}} '
    "statusCheckRollup{contexts(first:100){"
    "nodes{...on CheckRun{name conclusion completedAt} ...on StatusContext{context state createdAt}} "
    "pageInfo{hasNextPage}}} checkSuites(first:100){nodes{status workflowRun{databaseId createdAt}} "
    "pageInfo{hasNextPage}}}}}}}}"
)


@dataclass(frozen=True)
class PullRequest:
    state: str
    merged_at: int | None
    pushed_at: int | None
    red: bool
    resolved: bool = False
    failed: tuple = ()
    head: str = ""
    red_at: int | None = None
    unpassed_gate: str = ""
    queued: bool = False
    gate_passed: bool = False


def iso_ms(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def pull_request(raw):
    commits, checks = raw.get("commits") or [], raw.get("statusCheckRollup") or []
    suites = raw.get("checkSuites") or []
    results = [check.get("conclusion") or check.get("state") for check in checks]
    running = any(suite.get("workflowRun") and suite.get("status") != "COMPLETED" for suite in suites)
    pushes = [
        iso_ms(suite["workflowRun"]["createdAt"])
        for suite in suites
        if (suite.get("workflowRun") or {}).get("createdAt")
    ]
    reds = [
        iso_ms(check.get("completedAt") or check["createdAt"])
        for check, result in zip(checks, results)
        if result in RED and (check.get("completedAt") or check.get("createdAt"))
    ]
    if not pushes and commits:
        pushes = [iso_ms(commits[-1]["committedDate"])]
    unpassed_gate = _unpassed_gate(raw.get("gated"), checks, results, running)
    return PullRequest(
        raw["state"],
        iso_ms(raw["mergedAt"]) if raw.get("mergedAt") else None,
        min(pushes, default=None),
        any(result in RED for result in results) or bool(unpassed_gate),
        _resolved(raw.get("gated"), checks, results, running) or bool(unpassed_gate),
        tuple(
            check.get("name") or check.get("context") or "a check"
            for check, result in zip(checks, results)
            if result in RED
        ),
        raw.get("headRefOid") or "",
        min(reds, default=None),
        unpassed_gate,
        raw.get("mergeQueueEntry") is not None,
        bool(raw.get("gated")) and set(_gate(checks, results)) == {"SUCCESS"},
    )


def red_window(pushed_at, red_at, now_ms):
    start = max((mark for mark in (pushed_at, red_at) if mark is not None), default=None)
    return start if start is not None and now_ms - start >= RED_QUIET_MS else None


def _unpassed_gate(gated, checks, results, running):
    if not gated or running or any(result in PENDING for result in results):
        return ""
    gate = _gate(checks, results)
    return GATE if all(result == "SKIPPED" for result in gate) else ""


def _resolved(gated, checks, results, running):
    if not running and any(result in FINAL_RED for result in results):
        return True
    if not gated:
        return not running and "SUCCESS" in results and all(result in PASSED for result in results)
    gate = _gate(checks, results)
    if any(result in FINAL_RED for result in gate):
        return True
    return "SUCCESS" in gate and not running and not any(result in PENDING for result in results)


def _gate(checks, results):
    return [result for check, result in zip(checks, results) if (check.get("name") or check.get("context")) == GATE]


def declares_gate(tree):
    entries = ((tree or {}).get("object") or {}).get("entries") or []
    texts = [(entry.get("object") or {}).get("text") for entry in entries]
    return any(GATE_JOB.search(text) for text in texts if text)


def view(url, run=subprocess.run):
    try:
        done = run(
            ["gh", "api", "graphql", "-f", f"query={PULL_QUERY}", "-f", f"url={url}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if done.returncode != 0:
            return None
        return _view_resource(json.loads(done.stdout)["data"]["resource"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def _view_resource(raw, gated=None):
    try:
        raw = dict(raw)
        commits = [node["commit"] for node in raw["commits"]["nodes"]]
        rollup = (commits[-1].get("statusCheckRollup") or {}) if commits else {}
        contexts = rollup.get("contexts") or {}
        suites = commits[-1]["checkSuites"] if commits else {"nodes": [], "pageInfo": {}}
        if contexts.get("pageInfo", {}).get("hasNextPage") or suites["pageInfo"].get("hasNextPage"):
            return None
        raw["commits"] = commits
        raw["statusCheckRollup"] = contexts.get("nodes") or []
        raw["checkSuites"] = list(suites["nodes"])
        raw["gated"] = gated if gated is not None else bool(commits) and declares_gate(commits[-1].get("file"))
        return pull_request(raw)
    except (ValueError, KeyError, TypeError):
        return None


def views(
    urls: list[str], run: Callable = subprocess.run, cache: object | None = None
) -> dict[str, PullRequest | None]:
    urls = list(dict.fromkeys(urls))
    found = dict.fromkeys(urls)
    selection = PULL_QUERY.partition("{")[2][:-1].replace(
        'file(path:".github/workflows"){object{...on Tree{entries{object{...on Blob{text}}}}}}',
        'file(path:".github/workflows"){object{id}}',
    )
    for start in range(0, len(urls), 20):
        batch = urls[start : start + 20]
        fields = [f"p{i}:" + selection.replace("$url", json.dumps(url)) for i, url in enumerate(batch)]
        data = _graphql("{" + " ".join(fields) + "}", run)
        gates = _workflow_gates(data, run, cache)
        for i, url in enumerate(batch):
            raw = data.get(f"p{i}")
            gated = gates.get(_workflow_id(raw))
            found[url] = _view_resource(raw, gated) if gated is not None else None
    return found


def _graphql(query, run):
    try:
        done = run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        result = json.loads(done.stdout)
        errors = result.get("errors", [])
        if done.returncode and not errors:
            return {}
        data = result["data"]
        if not isinstance(data, dict):
            return {}
        for error in errors:
            path = error.get("path")
            if not path:
                return {}
            data[path[0]] = None
        return data
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return {}


def _workflow_id(raw):
    try:
        commits = raw["commits"]["nodes"]
        tree = commits[-1]["commit"]["file"] if commits else None
        return tree["object"]["id"] if tree is not None else None
    except (KeyError, TypeError):
        return ""


def _workflow_gates(data, run, cache):
    trees = {_workflow_id(raw) for raw in data.values()} - {None, ""}
    gates = {None: False, "": None, **dict.fromkeys(trees)}
    pending = []
    for tree in sorted(trees):
        saved = cache.get(f"{PREFIX}:workflow-gate:{tree}") if cache is not None else None
        if saved in ("0", "1"):
            gates[tree] = saved == "1"
        else:
            pending.append(tree)
    if pending:
        query = "{nodes(ids:" + json.dumps(pending) + "){id ...on Tree{entries{object{...on Blob{text}}}}}}"
        for node in _graphql(query, run).get("nodes") or []:
            if node is None or "entries" not in node:
                continue
            tree = node["id"]
            gates[tree] = declares_gate({"object": node})
            if cache is not None:
                cache.set(f"{PREFIX}:workflow-gate:{tree}", int(gates[tree]), ex=SENT_TTL_S)
    return gates


def tick_view(inbox: object, store: object, slug: str, doc: dict) -> Callable[[str], PullRequest | None]:
    urls = [
        task["pr_url"] for task in doc.get("tasks", []) if task.get("state") in ("pr", "claimed") and task.get("pr_url")
    ]
    notices = store.redis.hgetall(store.key(slug, "red-notices"))
    urls += [url for item_id, url in notices.items() if _open(inbox, item_id)]
    snapshots = views(urls, cache=store.redis)

    @functools.cache
    def lookup(url):
        return snapshots[url] if url in snapshots else view(url)

    return lookup


class Mail:
    def __init__(self, inbox, store, slug, doc=None):
        self.inbox, self.store, self.slug, self.doc = inbox, store, slug, doc or {}
        live = [a for a in store.agents(slug) if a.state != "finished"]
        self.seats = {a.name: a.seat or a.name for a in live}
        self.has_master = any(a.lane == MASTER for a in live)
        self.master = operator_mail.master_address(slug, live)
        self.masters = masters.live_seats(live)

    @functools.cached_property
    def owners(self):
        return masters.MasterSeats(self.store.redis).owners(self.slug, self.doc) if self.doc else {}

    def owner(self, target):
        """A phase's or a task's item goes to the live master owning its phase, anything else to the lead."""
        return masters.route(target, self.doc, self.owners, self.masters, self.master)

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

    def send_red(self, key, address, text, url):
        if self.once(key, lambda: self.track_red(self.inbox.send(SENDER, address, text), url)):
            return [f"told {address}: {key}"]
        return []

    def red_index(self):
        return self.store.key(self.slug, "red-notices")

    def track_red(self, item, url):
        self.store.redis.hset(self.red_index(), item.id, url)

    def engineer(self, task):
        return self.seats.get(task.get("claimed_by", "")) or self.owner(f"tasks/{task.get('id', '')}")


def event_pass(inbox, store, slug, doc, ledger, now_ms, github=view):
    mail, github = Mail(inbox, store, slug, doc), functools.cache(github)
    events = doc.get("_meta", {}).get("events", [])
    tasks = {t["id"]: t for t in doc.get("tasks", [])}
    raised = _raised_for_operator(doc, events)
    return (
        _events(mail, new_events(store, slug, doc, "events-cursor"), tasks, raised)
        + _followups(mail, doc, events, ledger, now_ms)
        + _pull_requests(mail, tasks.values(), now_ms, github)
        + _settle_red_notices(mail, github)
        + _priorities(mail, doc, raised)
    )


def _raised_for_operator(doc, events):
    """Follow-ups the swarm itself added for the operator: a master notice about one would escalate into another."""
    added = {e.get("target") for e in events if e.get("kind") == "added" and e.get("by") == SENDER}
    return {f"followups/{f['id']}" for f in doc.get("followups", []) if f.get("needs_operator")} & added


def _settle_red_notices(mail, github):
    mail.once("red-notices:backfill", lambda: _backfill_red_notices(mail))
    index, settled = mail.red_index(), []
    for item_id, url in mail.store.redis.hgetall(index).items():
        if not _open(mail.inbox, item_id):
            mail.store.redis.hdel(index, item_id)
            continue
        how = _settled(github(url))
        if not how:
            continue
        try:
            mail.inbox.close(item_id, SENDER, "done", f"pull request {url} {how}")
        except InboxError:
            continue
        settled.append(f"closed the red notice {item_id}: {url} {how}")
        mail.store.redis.hdel(index, item_id)
    return settled


def _open(inbox, item_id):
    try:
        return inbox.get(item_id).state not in CLOSED
    except InboxError:
        return False


def _backfill_red_notices(mail):
    prefix = mail.inbox.key("address", "")
    for key in mail.inbox.redis.scan_iter(match=f"{prefix}*@{mail.slug}"):
        for item in mail.inbox.inbox(key[len(prefix) :]):
            found = RED_NOTICE.match(item.text)
            if item.sender == SENDER and item.state not in CLOSED and found:
                mail.track_red(item, found.group(1))


def _settled(found):
    if found is None:
        return ""
    if found.state == "MERGED":
        return "merged"
    if found.state == "CLOSED":
        return "closed"
    return "turned green" if found.resolved and not found.red else ""


def findings_pass(inbox, store, slug, shown):
    mail, sent = Mail(inbox, store, slug), []
    for found in shown:
        if found["kind"] == "spawn stall":
            continue
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


def _events(mail, events, tasks, raised):
    sent = []
    for event in filter(lambda e: _by_agent(mail, e) and e.get("target") not in raised, events):
        text = _describe(mail.slug, event, tasks)
        if text:
            sent += mail.send(
                f"event:{event['rev']}:{event['kind']}:{event['target']}",
                mail.owner(event["target"]),
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


def _priorities(mail, doc, raised):
    key = mail.store.key(mail.slug, "priorities-sent")
    rows = {p["item"]: p for p in doc.get("priorities", [])}
    seen = mail.store.redis.smembers(key)
    gone = seen - rows.keys()
    if gone:
        mail.store.redis.srem(key, *gone)
    if not mail.has_master:
        return []
    marker = mail.store.key(mail.slug, "priorities-seeded")
    seeding, sent = not mail.store.redis.exists(marker), []
    for item, row in rows.items():
        if item in seen or row.get("by") == SENDER or not _by_agent(mail, row):
            continue
        if not seeding and item not in raised:
            text = (
                f"New priority on ledger {mail.slug} for {item}: {row['text']}\n"
                "Triage it: resolve it if the call is yours, else leave it for the operator."
            )
            mail.inbox.send(SENDER, mail.owner(item), text, ref=f"{mail.slug}:priority:{item}")
            sent.append(f"told {mail.owner(item)}: priority {item}")
        mail.store.redis.sadd(key, item)
    mail.store.redis.set(marker, 1)
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
                sent += mail.send(f"{url}:merged:master", mail.owner(f"tasks/{task['id']}"), text)
        elif found.state == "CLOSED":
            text = f"Your pull request {url} for {title} was closed without merging. Reopen it, open a new one, or block the task."
            sent += mail.send(f"{url}:closed", mail.engineer(task), text)
        elif (
            found.red
            and not found.gate_passed
            and (not found.unpassed_gate or found.failed)
            and red_window(found.pushed_at, found.red_at, now_ms) is not None
        ):
            text = (
                f"Your pull request {url} for {title} has red checks and no push for twenty minutes. Fix them and push."
            )
            sent += mail.send_red(f"{url}:red:{found.head}", mail.engineer(task), text, url)
    return sent
