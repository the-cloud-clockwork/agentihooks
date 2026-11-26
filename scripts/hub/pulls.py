import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from scripts.events.bus import Bus, StateConflict, StateStore
from scripts.events.envelope import Envelope
from scripts.events.subjects import subject
from scripts.hub import lease
from scripts.hub.registry import Reactor
from scripts.swarm import merge_queue, run_wait
from scripts.swarm.store import SwarmError

BUCKET = "hub-github"
OPEN = "open"
POLLED = "polled"
SWEEP = "sweep"
HEALTH = "health"
SLICE_MS = 10_000
INTERVAL_MS = 300_000
FLUSH_AFTER_MS = 60_000
FETCH_ERRORS = (SwarmError, KeyError, TypeError)
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
    culprit: dict[str, object] | None = None


def key(url: str) -> str:
    return "pull." + hashlib.sha256(url.lower().encode()).hexdigest()


def repository_key(repository: str) -> str:
    """Return the state key of the swarms tracking a pull request in ``repository``."""
    return "repo." + hashlib.sha256(repository.lower().encode()).hexdigest()


def _joined(swarm: str, swarms: Any) -> list[str] | None:
    """Return ``swarms`` with ``swarm`` added in order, or None when it is already there."""
    swarms = swarms or []
    return None if swarm in swarms else sorted([*swarms, swarm])


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
        early = snapshot["early"].get(seen.head, {})
        snapshot["head"], snapshot["suites"], snapshot["early"] = seen.head, {}, {}
        found += _suites(snapshot, early)
    if seen.state is not None and seen.state != snapshot["state"]:
        found.append((ARRIVED.get(seen.state) or ("pull.reopened" if snapshot["state"] else "pull.opened"), {}))
        snapshot["state"] = seen.state
    if snapshot["state"] in TERMINAL:
        snapshot["queued"] = False
    elif seen.queued is not None and seen.queued != snapshot["queued"]:
        named = {} if seen.culprit is None else {"culprit": seen.culprit}
        found.append(("merge.queued" if seen.queued else "merge.dequeued", named))
        snapshot["queued"] = seen.queued
    if seen.checks_head is not None and seen.checks_head == snapshot["head"]:
        found += _suites(snapshot, seen.suites)
    elif seen.checks_head is not None:
        snapshot["early"][seen.checks_head] = {**snapshot["early"].get(seen.checks_head, {}), **seen.suites}
    return found


def _suites(snapshot: dict, suites: dict) -> list[tuple[str, dict]]:
    found = []
    for suite, result in sorted(suites.items()):
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


def _resumed(urls: list[str], cursor: str | None) -> list[str]:
    """Return the open URLs from ``cursor`` on, or every one when the cursor has left the index."""
    return urls[urls.index(cursor) :] if cursor in urls else urls


class Pulls:
    def __init__(
        self, bus: Bus, fetch: Callable[[str], Observation] = fetch, clock: lease.Clock = lease.wall_ms
    ) -> None:
        self._events = bus.events
        self._state = bus.state(BUCKET)
        self._fetch = fetch
        self._clock = clock
        self._late = 0

    def reactor(self) -> Reactor:
        return Reactor("pulls", "swarm.*.task.*", self.handle, singleton=True, poll=self.poll)

    def handle(self, event: Envelope) -> None:
        url = event.data.get("pr_url")
        if event.type == "task.changed" and isinstance(url, str) and URL.fullmatch(url):
            self.track(event.swarm, url)
        self._late = max(self._late, self._clock() - event.time)

    def track(self, swarm: str, url: str) -> None:
        def add(snapshot):
            snapshot = snapshot or {
                "url": url,
                "swarms": [],
                "state": None,
                "head": None,
                "queued": False,
                "suites": {},
                "early": {},
                "version": 0,
                "pending": [],
            }
            if swarm in snapshot["swarms"]:
                return None
            return {**snapshot, "swarms": sorted([*snapshot["swarms"], swarm])}

        _update(self._state, key(url), add)
        _update(self._state, repository_key(url.rsplit("/pull/", 1)[0]), partial(_joined, swarm))
        stored = self._snapshot(url)
        if stored["state"] not in TERMINAL:
            self._index(stored["url"], True)

    def observe(self, seen: Observation) -> None:
        now = self._clock()
        created = []

        def change(snapshot):
            if snapshot is None:
                return None
            after = json.loads(json.dumps(snapshot))
            created[:] = _rows(after, _changes(after, seen), now)
            after["pending"] += created
            return None if after == snapshot else after

        written = _update(self._state, key(seen.url), change) or self._snapshot(seen.url)
        self._publish(seen.url, created)
        if written is not None:
            self._index(written["url"], written["state"] not in TERMINAL)

    def announce(self, repository: str, kind: str, ident: str, data: dict[str, object]) -> None:
        """Publish event ``kind`` about change ``ident`` of ``repository`` to every swarm tracking a pull request in
        it, under one id per swarm and change so a redelivery is the same event."""
        entry = self._state.get(repository_key(repository))
        now = self._clock()
        swarms: list[str] = json.loads(entry.value) if entry else []
        for swarm in swarms:
            digest = hashlib.sha256(json.dumps([swarm, kind, repository.lower(), ident]).encode()).hexdigest()
            entity = kind.split(".")[0]
            event = Envelope(digest, kind, swarm, entity, ident, 1, now, AUTHOR, {"repository": repository, **data})
            self._events.publish(subject(swarm, *kind.split(".")), event)

    def _snapshot(self, url: str) -> dict | None:
        entry = self._state.get(key(url))
        return None if entry is None else json.loads(entry.value)

    def _publish(self, url: str, rows: list[dict]) -> None:
        if not rows:
            return
        for row in rows:
            self._events.publish(row["subject"], Envelope.from_dict(row["event"]))
        sent = {row["event"]["id"] for row in rows}
        _update(
            self._state,
            key(url),
            lambda snapshot: {
                **snapshot,
                "pending": [row for row in snapshot["pending"] if row["event"]["id"] not in sent],
            },
        )

    def _index(self, url: str, present: bool) -> None:
        def change(urls):
            urls = urls or []
            if (url in urls) == present:
                return None
            return [*urls, url] if present else [other for other in urls if other != url]

        _update(self._state, OPEN, change)

    def poll(self, guard: Callable[[], bool] = lambda: True) -> None:
        """Sweep the open pull requests for at most ``SLICE_MS``, resuming the sweep a previous call paused.

        A sweep starts once ``INTERVAL_MS`` has passed since the last one started, and records its start in
        ``POLLED`` when it wraps; a rate limit ends it and waits a full interval from this call.
        """
        if not guard():
            return
        now = self._clock()
        sweep = self._sweep(now)
        if sweep is None:
            return
        polled = sweep["started"]
        index = self._state.get(OPEN)
        for url in _resumed(json.loads(index.value) if index else [], sweep.get("next")):
            if not guard():
                return
            if self._clock() >= now + SLICE_MS:
                self._pause(sweep, url)
                return
            pending = self._snapshot(url)["pending"]
            self._publish(url, [row for row in pending if row["event"]["time"] <= now - FLUSH_AFTER_MS])
            try:
                seen = self._fetch(url)
            except FETCH_ERRORS as exc:
                print(f"hub: GitHub poll skipped {url}: {exc}", file=sys.stderr)
                if run_wait.rate_limited(exc):
                    polled = now
                    break
                continue
            self.observe(seen)
        _update(self._state, POLLED, lambda _: polled)
        _update(self._state, SWEEP, lambda _: {})
        self._report({"sweep_ms": self._clock() - sweep["started"]})

    def _sweep(self, now: int) -> dict[str, Any] | None:
        """Return the sweep in progress, a new one starting at ``now`` once the interval has passed, or None."""
        entry = self._state.get(SWEEP)
        sweep: dict[str, Any] = json.loads(entry.value) if entry else {}
        if sweep:
            return sweep
        polled = self._state.get(POLLED)
        if polled is not None and now < json.loads(polled.value) + INTERVAL_MS:
            return None
        return {"started": now}

    def _pause(self, sweep: dict[str, Any], url: str) -> None:
        """Store ``url`` as the next pull request of ``sweep`` and report the track delay so far."""
        _update(self._state, SWEEP, lambda _: {**sweep, "next": url})
        self._report({})

    def _report(self, extra: dict[str, int]) -> None:
        """Write the oldest task event age handled since the last report, with ``extra``, to ``HEALTH``."""
        late, self._late = self._late, 0
        at = self._clock()
        _update(self._state, HEALTH, lambda value: {**(value or {}), "at": at, "track_delay_ms": late, **extra})
