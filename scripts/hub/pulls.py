import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from scripts.events.bus import Bus, StateConflict, StateStore
from scripts.events.envelope import Envelope
from scripts.events.subjects import subject
from scripts.hub import lease
from scripts.hub.registry import Reactor
from scripts.swarm import merge_queue
from scripts.swarm.store import SwarmError

BUCKET = "hub-github"
OPEN = "open"
POLLED = "polled"
INTERVAL_MS = 300_000
ATTEMPTS = 8
AUTHOR = "github"
ENTITY = "pull"
URL = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[0-9]+")
STATES = {"OPEN": "open", "CLOSED": "closed", "MERGED": "merged"}
TERMINAL = frozenset({"closed", "merged"})
ARRIVED = {"merged": "pull.merged", "closed": "pull.closed"}
QUERY = """query($url: URI!) { resource(url: $url) { ... on PullRequest {
    state headRefOid mergeQueueEntry { id }
    commits(last: 1) { nodes { commit { checkSuites(first: 100) {
        nodes { databaseId status conclusion app { slug } }
    } } } }
} } }"""


@dataclass(frozen=True)
class Observation:
    url: str
    state: str | None = None
    head: str | None = None
    queued: bool | None = None
    checks_head: str | None = None
    suites: dict = field(default_factory=dict)


def key(url: str) -> str:
    return "pull." + hashlib.sha256(url.encode()).hexdigest()


def suite_result(app: dict | None, conclusion: str) -> dict:
    return {"app": (app or {"slug": ""})["slug"], "conclusion": conclusion.lower()}


def fetch(url: str, run: Callable | None = None) -> Observation:
    pull = merge_queue.graphql(QUERY, {"url": url}, run or subprocess.run)["resource"]
    if pull is None:
        raise SwarmError(f"GitHub has no pull request at {url}")
    commits = pull["commits"]["nodes"]
    suites = commits[0]["commit"]["checkSuites"]["nodes"] if commits else []
    completed = {
        str(suite["databaseId"]): suite_result(suite["app"], suite["conclusion"])
        for suite in suites
        if suite["status"] == "COMPLETED"
    }
    head = pull["headRefOid"]
    return Observation(url, STATES[pull["state"]], head, pull["mergeQueueEntry"] is not None, head, completed)


def _update(state: StateStore, name: str, change: Callable[[object], object]) -> object:
    for _ in range(ATTEMPTS):
        entry = state.get(name)
        value = change(None if entry is None else json.loads(entry.value))
        if value is None:
            return None
        encoded = json.dumps(value).encode()
        try:
            if entry is None:
                state.create(name, encoded)
            else:
                state.update(name, encoded, entry.revision)
            return value
        except StateConflict:
            continue
    raise StateConflict("hub github state remained contended")


def _changes(snapshot: dict, seen: Observation) -> list[tuple[str, dict]]:
    found = []
    if seen.head is not None and seen.head != snapshot["head"]:
        if snapshot["head"] is not None:
            found.append(("pull.pushed", {"previous": snapshot["head"]}))
        snapshot["head"], snapshot["suites"] = seen.head, {}
    if seen.state is not None and seen.state != snapshot["state"]:
        found.append((ARRIVED.get(seen.state) or ("pull.reopened" if snapshot["state"] else "pull.opened"), {}))
        snapshot["state"] = seen.state
    if snapshot["state"] in TERMINAL:
        snapshot["queued"] = False
    elif seen.queued is not None and seen.queued != snapshot["queued"]:
        found.append(("merge.queued" if seen.queued else "merge.dequeued", {}))
        snapshot["queued"] = seen.queued
    if seen.checks_head is not None and seen.checks_head == snapshot["head"]:
        for suite, result in sorted(seen.suites.items()):
            if snapshot["suites"].get(suite) != result:
                found.append(("checks.completed", {"suite": suite, **result}))
                snapshot["suites"][suite] = result
    return found


def _rows(snapshot: dict, found: list[tuple[str, dict]], now: int) -> list[dict]:
    rows = []
    url = snapshot["url"]
    for kind, extra in found:
        snapshot["version"] += 1
        data = {"url": url, "state": snapshot["state"], "head": snapshot["head"], "queued": snapshot["queued"], **extra}
        for swarm in snapshot["swarms"]:
            digest = hashlib.sha256(json.dumps([swarm, url, snapshot["version"]]).encode()).hexdigest()
            event = Envelope(digest, kind, swarm, ENTITY, url, snapshot["version"], now, AUTHOR, data)
            rows.append({"subject": subject(swarm, *kind.split(".")), "event": event.to_dict()})
    return rows


class Pulls:
    def __init__(self, bus: Bus, fetch: Callable[[str], Observation] = fetch, clock: lease.Clock = lease.wall_ms):
        self._events = bus.events
        self._state = bus.state(BUCKET)
        self._fetch = fetch
        self._clock = clock

    def reactor(self) -> Reactor:
        return Reactor("pulls", "swarm.*.task.changed", self.handle, singleton=True, poll=self.poll)

    def handle(self, event: Envelope) -> None:
        url = event.data.get("pr_url")
        if isinstance(url, str) and URL.fullmatch(url):
            self.track(event.swarm, url)

    def track(self, swarm: str, url: str) -> None:
        def add(snapshot):
            snapshot = snapshot or {
                "url": url,
                "swarms": [],
                "state": None,
                "head": None,
                "queued": False,
                "suites": {},
                "version": 0,
                "pending": [],
            }
            if swarm in snapshot["swarms"]:
                return None
            return {**snapshot, "swarms": sorted([*snapshot["swarms"], swarm])}

        _update(self._state, key(url), add)
        if json.loads(self._state.get(key(url)).value)["state"] not in TERMINAL:
            self._index(url, True)

    def observe(self, seen: Observation) -> None:
        now = self._clock()

        def change(snapshot):
            if snapshot is None:
                return None
            after = json.loads(json.dumps(snapshot))
            found = _changes(after, seen)
            if not found and not after["pending"]:
                return None
            after["pending"] += _rows(after, found, now)
            return after

        written = _update(self._state, key(seen.url), change)
        if written is None:
            return
        for row in written["pending"]:
            self._events.publish(row["subject"], Envelope.from_dict(row["event"]))
        sent = {row["event"]["id"] for row in written["pending"]}
        _update(
            self._state,
            key(seen.url),
            lambda snapshot: {
                **snapshot,
                "pending": [row for row in snapshot["pending"] if row["event"]["id"] not in sent],
            },
        )
        if written["state"] in TERMINAL:
            self._index(seen.url, False)

    def _index(self, url: str, present: bool) -> None:
        def change(urls):
            urls = urls or []
            if (url in urls) == present:
                return None
            return [*urls, url] if present else [other for other in urls if other != url]

        _update(self._state, OPEN, change)

    def poll(self, guard: Callable[[], bool] = lambda: True) -> None:
        if not guard():
            return
        entry = self._state.get(POLLED)
        now = self._clock()
        if entry is not None and now < json.loads(entry.value) + INTERVAL_MS:
            return
        index = self._state.get(OPEN)
        for url in json.loads(index.value) if index else []:
            if not guard():
                return
            try:
                seen = self._fetch(url)
            except SwarmError as exc:
                print(f"hub: GitHub poll stopped at {url}: {exc}", file=sys.stderr)
                break
            self.observe(seen)
        _update(self._state, POLLED, lambda _: now)
