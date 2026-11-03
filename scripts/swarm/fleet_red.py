"""Hold the claim of a task whose pull request is red only from checks failing on most other open pull requests."""

import json
import re
import sys
import time
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any, Protocol, cast

from scripts.swarm import panic
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.ledger_events import RED, iso_ms
from scripts.swarm.stack import PORT_ERRORS
from scripts.swarm.store import RedisStore
from scripts.tick.deadline import Deadline
from scripts.tick.ports.contracts import SourceControl
from scripts.tick.ports.records import RevisionConflict
from scripts.tick.ports.source_control import OpenChange, RollupCheck
from scripts.tick.step import Domain, Write

MIN_OTHERS = 3
PULL = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/\d+")
WINDOW_MS = 6 * 3_600_000
SECONDS = 60
TTL = 600
ERRORS = PORT_ERRORS
NOTICE = (
    "The swarm holds this task unclaimed: its pull request {url} is red only from {checks}, which also fail on "
    "most other open pull requests. It is claimed again once those checks stop failing elsewhere."
)

LISTINGS = Domain("fleet red listings")
HOLDS = Domain("fleet red holds")
RELEASES = Domain("fleet red releases")
DOMAINS = frozenset({HOLDS, LISTINGS, RELEASES})

Pulls = Sequence[OpenChange]
Reader = Callable[[str], Pulls]
Listed = tuple[Pulls | None, tuple[Write, ...]]
Hold = tuple[str, str, str, Pulls]


def failing(pull: OpenChange) -> set[str]:
    """Name the checks on an open pull request whose result is red."""
    return {check.name for check in pull.checks if check.result in RED}


def own(pulls: Pulls, url: str) -> set[str]:
    """Name the red checks on the listed pull request at the url, none when it is not listed."""
    return next((failing(p) for p in pulls if p.url == url), set())


def recent(pull: OpenChange, now_ms: int) -> bool:
    """Say whether any check on the pull request finished inside the window."""
    return any(iso_ms(check.at) >= now_ms - WINDOW_MS for check in pull.checks if check.at)


def repository(url: str) -> str:
    found = PULL.match(url)
    return found.group(1) if found else ""


def read(repo: str, ports: Callable[[str], SourceControl] = panic.port) -> Pulls:
    """List the repository's open pull requests with their check rollups through its source control port."""
    return ports(repo).open_changes(deadline=Deadline.after(SECONDS))


class Saved(Protocol):
    """The Redis calls the listing cache makes."""

    def get(self, name: str) -> str | bytes | None:
        """Return the value saved under the name, none when nothing is saved."""
        ...

    def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> object:
        """Save the value under the name, for ex seconds when ex is given, and only while nothing is saved there when nx
        holds."""
        ...

    def mget(self, keys: list[str]) -> list[str | bytes | None]:
        """Return the value saved under each key, none where nothing is saved."""
        ...

    def delete(self, *names: str) -> object:
        """Forget the values saved under the names."""
        ...


class Listings(Protocol):
    """The swarm store parts the listing cache reads."""

    redis: Saved

    def key(self, slug: str, *parts: str) -> str:
        """Name a swarm key."""
        ...


def cached(store: Listings, slug: str, repo: str, reader: Reader | None = None) -> Listed:
    """Return the repository's open pull requests saved in the last ten minutes with no save, else list them.

    A fresh listing comes back with the write that saves it, a failed listing as none; the engine's applier saves it.
    """
    key = store.key(slug, f"fleet-red-pulls:{repo}")
    saved = store.redis.get(key)
    if saved is not None:
        rows = json.loads(saved)
        if rows is None:
            return None, ()
        return tuple(OpenChange(url, tuple(RollupCheck(*check) for check in checks)) for url, checks in rows), ()
    try:
        pulls: Pulls | None = tuple((reader or read)(repo))
    except ERRORS:
        pulls = None
    listing = None if pulls is None else [[p.url, [[c.name, c.result, c.at] for c in p.checks]] for p in pulls]
    return pulls, (Write(LISTINGS, key, json.dumps(listing)),)


def saver(store: Listings) -> Callable[[Write], None]:
    """Return the listings applier: it saves a listing for ten minutes only while no listing is saved under its key."""
    return partial(_settled, partial(_save, store))


def _settled(apply: Callable[[Write], None], write: Write) -> None:
    """Apply ``write``, turning a Redis timeout into a dropped write the next pass recomputes, as the step skipped it."""
    from redis.exceptions import TimeoutError as RedisTimeoutError

    try:
        apply(write)
    except RedisTimeoutError as exc:
        raise RevisionConflict(f"Redis timed out applying {write.domain} {write.key}") from exc


def _save(store: Listings, write: Write) -> None:
    """Save the listing of ``write`` unless another pass saved one since the step read none."""
    if not store.redis.set(write.key, cast(str, write.value), ex=TTL, nx=True):
        raise RevisionConflict(f"another pass saved the listing under {write.key} since the step read none")


def _unsaved(reader: Reader, repo: str) -> Listed:
    """Return the reader's listing of ``repo`` with nothing to save."""
    return tuple(reader(repo)), ()


def shared(pulls: Pulls, url: str, now_ms: int) -> set[str]:
    others = [failing(p) for p in pulls if p.url != url and recent(p, now_ms)]
    if len(others) < MIN_OTHERS:
        return set()
    return {name for name in own(pulls, url) if sum(name in other for other in others) * 2 > len(others)}


def held(pulls: Pulls, url: str, now_ms: int) -> bool:
    mine = own(pulls, url)
    return bool(mine) and mine <= shared(pulls, url, now_ms)


def checker(reader: Reader | None = None, clock: Callable[[], float] = time.time) -> Callable[[dict[str, Any]], bool]:
    """Return the claim filter that holds an open task whose pull request is red only from fleet wide checks."""
    known: dict[str, Pulls] = {}

    def admits(task: dict[str, Any]) -> bool:
        """Admit the task unless its pull request is red only from checks failing on most other open ones."""
        url = task.get("pr_url") or ""
        repo = repository(url)
        if task.get("state") != "open" or not repo:
            return True
        if repo not in known:
            try:
                known[repo] = (reader or read)(repo)
            except ERRORS as exc:
                print(f"fleet red hold skipped, listing open pull requests failed: {exc}", file=sys.stderr)
                known[repo] = []
        return not held(known[repo], url, int(clock() * 1000))

    return admits


def notice_pass(
    slug: str,
    store: RedisStore,
    rows: dict[str, dict[str, Any]],
    now_ms: int,
    reader: Reader | None = None,
) -> list[str | Write]:
    """Return a hold and its action per unclaimed open task newly held by fleet red, the release of holds that lifted
    and the listing saves, writing nothing itself.

    The engine's appliers mark each hold and post its notice, release the lifted holds and save the listings.
    """
    unclaimed = [
        task["id"]
        for task in rows.values()
        if task.get("state") == "open" and task.get("pr_url") and not task.get("claimed_by")
    ]
    waiting = set(unclaimed) - store.claimed(slug, unclaimed)
    cleared = [store.key(slug, f"fleet-red:{task['id']}") for task in rows.values() if task["id"] not in waiting]
    listings: dict[str, Pulls | None] = {}
    fetch: Callable[[str], Listed] = partial(_unsaved, reader) if reader else partial(cached, store, slug)
    found: list[str | Write] = []
    holds: list[Hold] = []
    for task_id in [task_id for task_id in unclaimed if task_id in waiting]:
        url, key = rows[task_id]["pr_url"], store.key(slug, f"fleet-red:{task_id}")
        repo = repository(url)
        if repo not in listings:
            listings[repo], saves = _listed(fetch, repo)
            found.extend(saves)
        pulls = listings[repo]
        if pulls is None:
            continue
        if not held(pulls, url, now_ms):
            cleared.append(key)
            continue
        holds.append((task_id, url, key, pulls))
    for task_id, url, key, pulls in _unmarked(store, holds):
        found.extend(_hold_write(task_id, url, key, pulls))
    if cleared:
        found.append(Write(RELEASES, slug, tuple(cleared)))
    return found


def _listed(fetch: Callable[[str], Listed], repo: str) -> Listed:
    """Return the listing of ``repo`` and its saves, none for a failed listing and empty for a task without a repo."""
    if not repo:
        return (), ()
    try:
        return fetch(repo)
    except ERRORS:
        return None, ()


def _unmarked(store: Listings, holds: list[Hold]) -> list[Hold]:
    """Return the holds no pass has marked yet, reading every mark in one call and nothing without holds."""
    marks = store.redis.mget([key for _, _, key, _ in holds]) if holds else []
    return [hold for hold, mark in zip(holds, marks, strict=True) if mark is None]


def _hold_write(task_id: str, url: str, key: str, pulls: Pulls) -> list[str | Write]:
    """Return the hold of task ``task_id`` whose pull request is red only from fleet wide checks, and its action."""
    checks = ", ".join(sorted(own(pulls, url)))
    notice = NOTICE.format(url=url, checks=checks)
    return [
        Write(HOLDS, key, (task_id, url, notice)),
        f"task {task_id} held unclaimed, {url} is red only from {checks}",
    ]


def holder(store: Listings, ledger: Any, slug: str) -> Callable[[Write], None]:
    """Return the holds applier: it marks a hold and posts its notice only while no pass marked it yet."""
    return partial(_settled, partial(_hold, store, ledger, slug))


def _hold(store: Listings, ledger: Any, slug: str, write: Write) -> None:
    """Mark the hold of ``write`` and post its notice unless another pass marked it since the step read none.

    The mark is set before the notice, so a refused notice leaves the hold marked with no notice retried, as before,
    and raises RevisionConflict so the engine drops the write.
    """
    task_id, url, notice = cast(tuple[str, str, str], write.value)
    if not store.redis.set(write.key, url, nx=True):
        raise RevisionConflict(f"another pass marked the fleet red hold of task {task_id} since the step read none")
    try:
        ledger.notice(slug, task_id, notice)
    except LedgerRefused as exc:
        raise RevisionConflict(f"the ledger refused the fleet red notice of task {task_id}") from exc


def releaser(store: Listings) -> Callable[[Write], None]:
    """Return the releases applier: it forgets every lifted hold of a pass in one call."""
    return partial(_settled, partial(_release, store))


def _release(store: Listings, write: Write) -> None:
    """Forget the hold marks ``write`` names."""
    store.redis.delete(*cast(tuple[str, ...], write.value))
