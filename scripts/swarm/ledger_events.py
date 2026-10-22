"""Agent writes on a swarm ledger as inbox items, sent by the minute tick so the master hears of them asleep or awake.

New ledger events past the swarm's cursor go to the master owning their phase, else the lead. Time rules raise a
follow-up nobody decided, watch each task's pull request on GitHub and pass every new health finding on for a verdict.
Each item is sent once, so a replay of the same ledger sends nothing; the wake ladder then carries every item to a
reader.
"""

import collections
import contextlib
import functools
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from scripts.inbox.store import CLOSED, InboxError
from scripts.swarm import merge_queue, outage
from scripts.swarm.health.verdicts import VERDICTS
from scripts.swarm.naming import lane_of
from scripts.swarm.store import MASTER, PREFIX, SwarmError
from scripts.swarm_v2 import masters

MINUTE_MS = 60_000
TASK_FINDINGS = frozenset({"proof loop", "failed launch", "stale claim"})
HELD_FINDINGS = frozenset({"idle with claim", "waiting on input", "stalled", "working on drain"})
FOLLOWUP_RAISE_MS = 15 * MINUTE_MS
FOLLOWUP_OPERATOR_MS = 30 * MINUTE_MS
MERGED_ENGINEER_MS = 10 * MINUTE_MS
MERGED_MASTER_MS = 20 * MINUTE_MS
RED_QUIET_MS = 20 * MINUTE_MS
SENT_TTL_S = 30 * 24 * 3600
SENDER = "swarm"
EVENTS_CURSOR = "events-cursor"
RELAYED = "agent follow ups, questions and task changes"
OPERATOR = "operator"
ASK_WORDS = 12
RED = {"FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}
FINAL_RED = RED - {"TIMED_OUT"}
PASSED = {"SUCCESS", "SKIPPED"}
PENDING = {None, "", "PENDING", "EXPECTED"}
GATE = "Gate — Required"
RED_NOTICE = re.compile(r"^Your pull request (\S+) for .* has red checks and no push for twenty minutes\.")
GATE_JOB = re.compile(rf"^[ \t]+name:[ \t]*(['\"]?){re.escape(GATE)}\1[ \t]*$", re.MULTILINE)
CONTEXT_NODES = (
    "nodes{...on CheckRun{databaseId name conclusion startedAt completedAt isRequired(pullRequestNumber:$number) "
    "checkSuite{app{databaseId} workflowRun{databaseId event workflow{id name resourcePath}}}} "
    "...on StatusContext{context state createdAt isRequired(pullRequestNumber:$number)}} "
    "pageInfo{hasNextPage endCursor}"
)
SUITE_NODES = "nodes{status conclusion workflowRun{databaseId createdAt event workflow{id resourcePath}}} pageInfo{hasNextPage endCursor}"
PULL_QUERY = (
    "query($url:URI!){resource(url:$url){...on PullRequest{state mergedAt headRefOid mergeQueueEntry{id state} "
    "mergeable isDraft body timelineItems(last:1,itemTypes:[REOPENED_EVENT,ADDED_TO_MERGE_QUEUE_EVENT])"
    "{nodes{...on ReopenedEvent{createdAt} ...on AddedToMergeQueueEvent{createdAt}}} "
    "commits(last:1){nodes{commit{id oid committedDate "
    'file(path:".github/workflows"){object{...on Tree{entries{name object{...on Blob{text}}}}}} '
    + "statusCheckRollup{contexts(first:100){"
    + CONTEXT_NODES
    + "}} checkSuites(first:100){"
    + SUITE_NODES
    + "}}}}}}}"
)
PAGE_PREFIX = "query($id:ID!,$after:String!){node(id:$id){...on Commit{"
PAGES = {
    "contexts": (
        PAGE_PREFIX + "statusCheckRollup{contexts(first:100,after:$after){" + CONTEXT_NODES + "}}}}}",
        ("statusCheckRollup", "contexts"),
    ),
    "checkSuites": (PAGE_PREFIX + "checkSuites(first:100,after:$after){" + SUITE_NODES + "}}}}", ("checkSuites",)),
}
MAX_PAGES = 20
ROLLUP_FALLBACK = "merged state alone, because GitHub refused the check rollup"
CHECK_FIELDS = frozenset({"statusCheckRollup", "checkSuites"})


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
    merge_problem: str = ""
    mergeable: str = ""
    draft: bool = False
    latest_run_cancelled: bool = False
    checks_pending: bool = False
    checks_passed: bool = False
    stale_red: bool = False
    required_pending: bool = False
    running: bool = False
    body: str = ""
    started: bool = False
    fallback: str = ""


def iso_ms(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def pull_request(raw):
    suites = raw.get("checkSuites") or []
    rollup = raw.get("statusCheckRollup") or []
    newest = _newest_runs(rollup, suites)
    everything = _newest([check for check in rollup if not _superseded(check, newest)])
    workflows = set(raw.get("gate_workflows") or [])
    commits, required = raw.get("commits") or [], _required(raw.get("gated"), rollup, workflows)
    checks = _newest([check for check in required if not _superseded(check, newest)])
    results = [check.get("conclusion") or check.get("state") for check in checks]
    required_runs = {_run_key(check) for check in required if _run_key(check)[0]}
    required_runs.update(
        _run_key(suite) for suite in suites if _workflow_file(suite) in workflows and _run_key(suite)[0]
    )
    present = {_workflow_file(item) for item in [*rollup, *suites]}
    expected = {
        (_run_key(check), check.get("name")) for check in rollup if check.get("isRequired") or check.get("name") == GATE
    }
    reported = {(_run_key(check), check.get("name")) for check in checks}
    started = any(
        suite.get("workflowRun")
        and suite.get("status") != "COMPLETED"
        and not _superseded(suite, newest)
        and (not raw.get("gated") or not required_runs or _run_key(suite) in required_runs)
        for suite in suites
    )
    running = started or bool(
        expected - reported or required_runs - {key for key, _ in reported} or workflows - present
    )
    required_pending = any(result in PENDING - {"EXPECTED"} for result in results)
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
    failing = any(result in RED for result in results)
    return PullRequest(
        raw["state"],
        iso_ms(raw["mergedAt"]) if raw.get("mergedAt") else None,
        min(pushes, default=None),
        failing or bool(unpassed_gate),
        _resolved(raw.get("gated"), checks, results, running) or bool(unpassed_gate),
        tuple(
            check.get("name") or check.get("context") or "a check"
            for check in everything
            if (check.get("conclusion") or check.get("state")) in RED
        )
        if failing
        else (),
        _read_head(raw.get("headRefOid") or "", commits),
        min(reds, default=None),
        unpassed_gate,
        raw.get("mergeQueueEntry") is not None,
        bool(raw.get("gated")) and set(_gate(checks, results)) == {"SUCCESS"},
        "has a merge conflict"
        if raw.get("mergeable") == "CONFLICTING"
        else "is marked unmergeable by GitHub"
        if (raw.get("mergeQueueEntry") or {}).get("state") == "UNMERGEABLE"
        else "",
        raw.get("mergeable") or "",
        bool(raw.get("isDraft")),
        _latest_run_cancelled(suites),
        any((check.get("conclusion") or check.get("state")) in (None, "PENDING", "EXPECTED") for check in everything),
        any((check.get("conclusion") or check.get("state")) == "SUCCESS" for check in everything)
        and all((check.get("conclusion") or check.get("state")) in PASSED | {"NEUTRAL"} for check in everything),
        _stale_red(raw, checks, results),
        required_pending,
        running or any(result in PENDING for result in results),
        raw.get("body") or "",
        started,
    )


def _stale_red(raw, checks, results):
    restarted = _restarted(raw)
    reds = [check for check, result in zip(checks, results) if result in RED]
    return (
        bool(reds)
        and restarted is not None
        and all(_started(check) is not None and _started(check) < restarted for check in reds)
    )


def _restarted(raw):
    events = ((raw.get("timelineItems") or {}).get("nodes")) or []
    return max((iso_ms(event["createdAt"]) for event in events if event and event.get("createdAt")), default=None)


def _started(check):
    started = check.get("startedAt") or check.get("createdAt")
    return iso_ms(started) if started else None


def _read_head(head, commits):
    read = (commits[-1].get("oid") if commits else None) or head
    return head if read == head else ""


def _newest(checks):
    latest = {}
    for order, check in sorted(enumerate(checks), key=_mark):
        workflow = (_workflow_run(check).get("workflow") or {}).get("name")
        latest[(workflow, _run_key(check), check.get("name"), check.get("context"))] = (order, check)
    return [check for _, check in sorted(latest.values())]


def _workflow_run(item):
    return (item.get("checkSuite") or item).get("workflowRun") or {}


def _newest_runs(checks, suites):
    newest = collections.defaultdict(int)
    for item in [*checks, *suites]:
        key, run = _run_key(item), _workflow_run(item).get("databaseId")
        if key[0] and run:
            newest[key] = max(newest[key], run)
    return newest


def _superseded(item, newest):
    run = _workflow_run(item).get("databaseId")
    return bool(run) and run < newest.get(_run_key(item), run)


def _run_key(item):
    run = _workflow_run(item)
    return (run.get("workflow") or {}).get("id"), run.get("event")


def _workflow_file(item):
    path = (_workflow_run(item).get("workflow") or {}).get("resourcePath")
    if path:
        return path.split("/")[-1]


def _required(gated, checks, workflows=()):
    required = [
        check
        for check in checks
        if check.get("isRequired", True) or (check.get("name") or check.get("context")) == GATE
    ]
    runs = {_run_key(check) for check in required if _run_key(check)[0]}
    runs.update(_run_key(check) for check in checks if _workflow_file(check) in workflows and _run_key(check)[0])
    return [check for check in checks if check in required or _run_key(check) in runs] if gated or required else checks


def _mark(item):
    order, check = item
    return check.get("databaseId") or 0, check.get("createdAt") or "", order


def _latest_run_cancelled(suites):
    runs = [suite for suite in suites if (suite.get("workflowRun") or {}).get("createdAt")]
    latest = max(
        runs,
        key=lambda suite: (suite["workflowRun"]["createdAt"], suite["workflowRun"].get("databaseId", 0)),
        default={},
    )
    return latest.get("conclusion") == "CANCELLED"


def red_window(pushed_at, red_at, now_ms):
    start = max((mark for mark in (pushed_at, red_at) if mark is not None), default=None)
    return start if start is not None and now_ms - start >= RED_QUIET_MS else None


def _unpassed_gate(gated, checks, results, running):
    if not gated or running or any(result in PENDING for result in results):
        return ""
    gate = _gate(checks, results)
    return GATE if all(result == "SKIPPED" for result in gate) else ""


def _resolved(gated, checks, results, running):
    if gated:
        return any(result in RED for result in results) or (
            not running
            and set(_gate(checks, results)) == {"SUCCESS"}
            and all(result in PASSED | {"NEUTRAL"} for result in results)
        )
    if not running and any(result in FINAL_RED for result in results):
        return True
    return not running and "SUCCESS" in results and all(result in PASSED for result in results)


def _gate(checks, results):
    return [result for check, result in zip(checks, results) if (check.get("name") or check.get("context")) == GATE]


def declares_gate(tree):
    return bool(_gate_files(tree))


def _gate_files(tree):
    entries = ((tree or {}).get("object") or {}).get("entries") or []
    return [
        entry.get("name")
        for entry in entries
        if (text := (entry.get("object") or {}).get("text")) and GATE_JOB.search(text)
    ]


def view(url, run=subprocess.run, required_checks=None):
    number = _number(url)
    try:
        done = run(
            ["gh", "api", "graphql", "-f", f"query={PULL_QUERY.replace('$number', number)}", "-f", f"url={url}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if done.returncode != 0:
            return _merged_without_rollup(json.loads(done.stdout))
        return _view_resource(
            json.loads(done.stdout)["data"]["resource"], number, run=run, required_checks=required_checks
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def _merged_without_rollup(result):
    errors, raw = result.get("errors"), result["data"]["resource"]
    if not errors or raw["state"] != "MERGED":
        return None
    if not all(error.get("type") == "FORBIDDEN" and CHECK_FIELDS.intersection(error["path"]) for error in errors):
        return None
    return PullRequest("MERGED", iso_ms(raw["mergedAt"]), None, False, head=raw["headRefOid"], fallback=ROLLUP_FALLBACK)


def head(url, run=subprocess.run):
    try:
        owner, repo, _, number = url.split("/")[-4:]
        return merge_queue.rest(f"repos/{owner}/{repo}/pulls/{number}", run)["head"]["sha"]
    except (SwarmError, ValueError, KeyError, TypeError):
        return ""


def _number(url):
    found = re.search(r"/pull/(\d+)", url)
    return found[1] if found else "0"


def _required_app(check, required_checks):
    app_id = required_checks.get(check.get("name") or check.get("context"))
    app = ((check.get("checkSuite") or {}).get("app") or {}).get("databaseId")
    return bool(check.get("context")) or app_id is None or app == app_id


def _required_pull_request(raw, required_checks):
    newest = _newest_runs(raw["statusCheckRollup"], raw["checkSuites"])
    checks = _newest(
        [
            check
            for check in raw["statusCheckRollup"]
            if check.get("isRequired") and _required_app(check, required_checks) and not _superseded(check, newest)
        ]
    )
    results = [check.get("conclusion") or check.get("state") for check in checks]
    return PullRequest(
        raw["state"],
        None,
        None,
        any(result in RED for result in results),
        resolved=set(required_checks).issubset(check.get("name") or check.get("context") for check in checks)
        and all(result in PASSED | {"NEUTRAL"} for result in results),
        failed=tuple(
            check.get("name") or check.get("context") or "a check"
            for check, result in zip(checks, results)
            if result in RED
        ),
        head=_read_head(raw["headRefOid"], raw.get("commits")),
    )


def _view_resource(raw, number, gated=None, run=subprocess.run, required_checks=None, gate_workflows=None):
    try:
        raw = dict(raw)
        commits = [node["commit"] for node in raw["commits"]["nodes"]]
        commit = commits[-1] if commits else {}
        contexts = (commit.get("statusCheckRollup") or {}).get("contexts") or {}
        contexts = {"nodes": contexts.get("nodes") or [], "pageInfo": contexts.get("pageInfo") or {}}
        suites = commit["checkSuites"] if commits else {"nodes": [], "pageInfo": {}}
        checks = _paged(commit.get("id"), "contexts", contexts, run, number)
        suites = _paged(commit.get("id"), "checkSuites", suites, run, number)
        if checks is None or suites is None:
            return None
        raw["commits"] = commits
        raw["statusCheckRollup"] = checks
        raw["checkSuites"] = suites
        if required_checks is not None:
            return _required_pull_request(raw, required_checks)
        files = _gate_files(commit.get("file")) if gate_workflows is None else gate_workflows
        raw["gated"] = gated if gated is not None else declares_gate(commit.get("file"))
        raw["gate_workflows"] = [name for name in files if name]
        return pull_request(raw)
    except (ValueError, KeyError, TypeError):
        return None


def _paged(commit, field, page, run, number):
    nodes, info = list(page["nodes"]), page["pageInfo"]
    for _ in range(MAX_PAGES):
        if not info.get("hasNextPage"):
            return nodes
        if not commit:
            return None
        query, path = PAGES[field]
        page = _graphql(query.replace("$number", number), run, id=commit, after=info["endCursor"]).get("node")
        for key in path:
            page = page[key]
        nodes, info = nodes + list(page["nodes"]), page["pageInfo"]
    return None if info.get("hasNextPage") else nodes


def views(
    urls: list[str], run: Callable = subprocess.run, cache: object | None = None
) -> dict[str, PullRequest | None]:
    urls = list(dict.fromkeys(urls))
    found = dict.fromkeys(urls)
    selection = PULL_QUERY.partition("{")[2][:-1].replace(
        'file(path:".github/workflows"){object{...on Tree{entries{name object{...on Blob{text}}}}}}',
        'file(path:".github/workflows"){object{id}}',
    )
    for start in range(0, len(urls), 20):
        batch = urls[start : start + 20]
        fields = [
            f"p{i}:" + selection.replace("$url", json.dumps(url)).replace("$number", _number(url))
            for i, url in enumerate(batch)
        ]
        data = _graphql("{" + " ".join(fields) + "}", run)
        gates = _workflow_gates(data, run, cache)
        for i, url in enumerate(batch):
            raw = data.get(f"p{i}")
            declaration = gates.get(_workflow_id(raw))
            found[url] = (
                _view_resource(raw, _number(url), declaration["gated"], run, gate_workflows=declaration["workflows"])
                if declaration is not None
                else None
            )
    return found


def _graphql(query, run, **variables):
    try:
        done = run(
            ["gh", "api", "graphql", "-f", f"query={query}"]
            + [arg for name, value in variables.items() for arg in ("-f", f"{name}={value}")],
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
    gates = {None: {"gated": False, "workflows": []}, **dict.fromkeys(trees)}
    pending = []
    for tree in sorted(trees):
        saved = cache.get(f"{PREFIX}:workflow-gate-files:{tree}") if cache is not None else None
        if saved is not None:
            gates[tree] = json.loads(saved)
        else:
            pending.append(tree)
    if pending:
        query = "{nodes(ids:" + json.dumps(pending) + "){id ...on Tree{entries{name object{...on Blob{text}}}}}}"
        for node in _graphql(query, run).get("nodes") or []:
            if node is None or "entries" not in node:
                continue
            tree = node["id"]
            files = _gate_files({"object": node})
            gates[tree] = {"gated": declares_gate({"object": node}), "workflows": [name for name in files if name]}
            if cache is not None:
                cache.set(f"{PREFIX}:workflow-gate-files:{tree}", json.dumps(gates[tree]), ex=SENT_TTL_S)
    return gates


def tick_view(inbox: object, store: object, slug: str, doc: dict) -> Callable[[str], PullRequest | None]:
    from scripts.swarm import metrics_outbox, metrics_swarm

    urls = [
        task["pr_url"] for task in doc.get("tasks", []) if task.get("state") in ("pr", "claimed") and task.get("pr_url")
    ]
    at = int(datetime.now().timestamp() * 1000)
    recent = metrics_outbox.read_recent("delivery_events", at)
    if recent is not None:
        urls.extend(url for url, _ in metrics_swarm.pull_targets(doc, at, recent))
    notices = store.redis.hgetall(store.key(slug, "red-notices"))
    urls += [url for item_id, url in notices.items() if _open(inbox, item_id)]
    snapshots = views(list(dict.fromkeys(urls)), cache=store.redis)

    @functools.cache
    def lookup(url):
        return snapshots[url] if url in snapshots else view(url)

    return lookup


class Mail:
    def __init__(self, inbox, store, slug, doc=None):
        self.inbox, self.store, self.slug, self.doc = inbox, store, slug, doc or {}
        agents = store.agents(slug)
        live = [a for a in agents if a.state != "finished"]
        self.seats = {a.name: a.seat or a.name for a in live}
        self.held = {a.name: a.task for a in agents if a.lane != MASTER}
        self.has_master = any(a.lane == MASTER for a in live)
        self.masters = list(masters.held(store.redis, slug, agents))
        self.master = masters.lead_of(slug, self.masters)

    @functools.cached_property
    def owners(self):
        return masters.MasterSeats(self.store.redis).owners(self.slug, self.doc) if self.doc else {}

    def owner(self, target):
        return masters.route(target, self.doc, lambda: self.owners, self.masters, self.master)

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

    def red_index(self):
        return self.store.key(self.slug, "red-notices")

    def track_red(self, item, url):
        self.store.redis.hset(self.red_index(), item.id, url)

    def engineer(self, task):
        return self.seats.get(task.get("claimed_by")) or self.owner(f"tasks/{task['id']}")

    def held_elsewhere(self, address, task):
        occupant = self.store.seats.occupant(address).occupant
        live = occupant in self.seats and occupant != task.get("claimed_by")
        return live and self.held.get(occupant) != task["id"]

    def task_index(self):
        return self.store.key(self.slug, "task-notices")


def event_pass(inbox, store, slug, doc, ledger, now_ms, github=view):
    mail, github = Mail(inbox, store, slug, doc), functools.cache(github)
    events = doc.get("_meta", {}).get("events", [])
    tasks = {t["id"]: t for t in doc.get("tasks", [])}
    raised = _raised_for_operator(doc, events)
    return (
        _events(mail, new_events(store, slug, doc, EVENTS_CURSOR, RELAYED), tasks, raised)
        + _history_gaps(mail)
        + _followups(mail, doc, events, ledger, now_ms)
        + _pull_requests(mail, tasks.values(), now_ms, github, ledger)
        + _settle_red_notices(mail, github)
        + _move_task_notices(mail, ledger, tasks)
        + _priorities(mail, doc, raised)
    )


def _history_gaps(mail):
    key, sent = mail.store.key(mail.slug, "history-gaps"), []
    for gap, value in mail.store.redis.hgetall(key).items():
        cursor, trimmed, subject = json.loads(value)
        text = (
            f"Ledger {mail.slug} trimmed its history after revision {cursor} through revision {trimmed} before the "
            f"tick read it, so {subject} in that range reached no one. Read the ledger for them."
        )
        sent += mail.send(f"history-gap:{gap}", mail.master, text)
        mail.store.redis.hdel(key, gap)
    return sent


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
    return _open_item(inbox, item_id) is not None


def _open_item(inbox, item_id):
    try:
        item = inbox.get(item_id)
    except InboxError:
        return None
    return None if item.state in CLOSED else item


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


def findings_pass(inbox, store, slug, shown, doc=None):
    from scripts.swarm.health import merge_stall

    mail, sent = Mail(inbox, store, slug, doc), merge_stall.deliver(inbox, store, slug)
    for found in shown:
        if found["kind"] == "merge stall":
            continue
        if found["kind"] in ("spawn stall", "stall") or found["kind"] in outage.HELD.values():
            continue
        judged = (found.get("verdict") or {}).get("at", 0)
        text = (
            f"New health finding on swarm {slug}: {found['summary']} ({found['kind']}). Give it a verdict: "
            f'agentihooks swarm {slug} verdict {found["id"]} {"|".join(VERDICTS)} --note "<why>"'
        )
        sent += mail.send(f"finding:{found['id']}:{judged}", _finding_owner(mail, found), text)
    return sent


def _finding_owner(mail, found):
    kind, subject = found["kind"], found["subject"]
    task = subject if kind in TASK_FINDINGS else kind in HELD_FINDINGS and mail.held.get(subject)
    return mail.owner(f"tasks/{task}") if task else mail.master


def new_events(store, slug, doc, cursor_name, subject):
    """Events past the named cursor, which moves to the ledger revision; history trimmed past it is kept as a gap."""
    key, rev = store.key(slug, cursor_name), doc["_meta"]["rev"]
    cursor = store.redis.get(key)
    store.redis.set(key, rev)
    if cursor is None:
        return []
    trimmed = doc["_meta"].get("events_trimmed")
    if trimmed is not None and trimmed > int(cursor):
        gap = json.dumps([cursor, trimmed, subject])
        store.redis.hset(store.key(slug, "history-gaps"), f"{cursor_name}:{cursor}:{trimmed}", gap)
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
            f"{by} added a follow-up {item} on ledger {slug}: {event.get('text', '')}\n"
            "Decide it: turn it into a task, close it with a status, or flag it for the operator.\n"
            f"Close it: agentihooks ledger --slug {slug} followup done {item}"
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
                f"A follow-up {followup['id']} on ledger {mail.slug} is still open fifteen minutes "
                f"after {event['by']} added it: {followup['text']}\n"
                "Decide it now, or it goes to the operator's Priorities.\n"
                f"Close it: agentihooks ledger --slug {mail.slug} followup done {followup['id']}"
            )
            sent += mail.send(f"{target}:raised", mail.owner(target), text, ref=f"{mail.slug}:event:{target}")
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
            owner = mail.owner(item)
            mail.inbox.send(SENDER, owner, text, ref=f"{mail.slug}:priority:{item}")
            sent.append(f"told {owner}: priority {item}")
        mail.store.redis.sadd(key, item)
    mail.store.redis.set(marker, 1)
    return sent


def _pull_requests(mail, tasks, now_ms, github, ledger):
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
                sent += _tell_engineer(mail, ledger, task, f"{url}:merged:engineer", text)
            if age >= MERGED_MASTER_MS:
                text = f"{title} on ledger {mail.slug} is still in pull request twenty minutes after {url} merged."
                sent += mail.send(f"{url}:merged:master", mail.owner(f"tasks/{task['id']}"), text)
        elif found.state == "CLOSED":
            text = f"Your pull request {url} for {title} was closed without merging. Reopen it, open a new one, or block the task."
            sent += _tell_engineer(mail, ledger, task, f"{url}:closed", text)
        elif (
            found.red
            and not found.gate_passed
            and (not found.unpassed_gate or found.failed)
            and red_window(found.pushed_at, found.red_at, now_ms) is not None
        ):
            text = (
                f"Your pull request {url} for {title} has red checks and no push for twenty minutes. Fix them and push."
            )
            sent += _tell_engineer(mail, ledger, task, f"{url}:red:{found.head}", text, url)
    return sent


def _tell_engineer(mail, ledger, task, key, text, url=""):
    seat = mail.seats.get(task.get("claimed_by"))
    if seat and mail.held_elsewhere(seat, task):
        if mail.once(key, lambda: ledger.comment(mail.slug, task["id"], text, by=SENDER)):
            return [f"noted on tasks/{task['id']}: {key}"]
        return []
    address = mail.engineer(task)

    def tell():
        item = mail.inbox.send(SENDER, address, text)
        if address == seat:
            mail.store.redis.hset(mail.task_index(), item.id, task["id"])
        if url:
            mail.track_red(item, url)

    return [f"told {address}: {key}"] if mail.once(key, tell) else []


def _move_task_notices(mail, ledger, tasks):
    index, moved = mail.task_index(), []
    for item_id, task_id in mail.store.redis.hgetall(index).items():
        task, item = tasks.get(task_id), _open_item(mail.inbox, item_id)
        if task is None or item is None:
            mail.store.redis.hdel(index, item_id)
            continue
        if not mail.held_elsewhere(item.address, task):
            continue
        ledger.comment(mail.slug, task_id, item.text, by=SENDER)
        mail.store.redis.hdel(index, item_id)
        with contextlib.suppress(InboxError):
            mail.inbox.close(
                item_id, SENDER, "done", f"moved onto task {task_id}; {item.address} now holds another task"
            )
        moved.append(f"moved {item_id} onto tasks/{task_id}")
    return moved
