"""Hold the claim of a task whose pull request is red only from checks failing on most other open pull requests."""

import json
import re
import sys
import time
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any, Protocol

from scripts.swarm import panic
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

Pulls = Sequence[OpenChange]
Reader = Callable[[str], Pulls]
Listed = tuple[Pulls | None, tuple[Write, ...]]


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


def read(repo: str, ports: Callable[[str], SourceControl] = panic.port, *, deadline: Deadline | None = None) -> Pulls:
    """List the repository's open pull requests through its source control port, bounded by the caller's deadline
    and the listing's own seconds, whichever ends first."""
    bound = Deadline.after(SECONDS) if deadline is None else deadline.child(SECONDS)
    return ports(repo).open_changes(deadline=bound)


class _Saved(Protocol):
    """The Redis calls the listing cache and its saver make."""

    def get(self, name: str) -> str | bytes | None:
        """Return the value saved under the name, none when nothing is saved."""
        ...

    def set(self, name: str, value: str, **kwargs: int | bool) -> object:
        """Save the value under the name, for ex seconds and only while nothing is saved there when nx holds."""
        ...


class Listings(Protocol):
    """The swarm store parts the listing cache reads."""

    redis: _Saved

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
        return _decoded(saved), ()
    try:
        pulls: Pulls | None = tuple((reader or read)(repo))
    except ERRORS:
        pulls = None
    listing = None if pulls is None else [[p.url, [[c.name, c.result, c.at] for c in p.checks]] for p in pulls]
    return pulls, (Write(LISTINGS, key, json.dumps(listing)),)


def _decoded(saved: str | bytes) -> Pulls | None:
    """Return the open pull requests of a saved listing, none when the save records a failed listing."""
    rows = json.loads(saved)
    if rows is None:
        return None
    return tuple(OpenChange(url, tuple(RollupCheck(*check) for check in checks)) for url, checks in rows)


def saved_reader(store: Listings, slug: str) -> Reader:
    """Return a reader serving only the repository's saved listing; nothing saved admits every task in it."""
    return partial(_saved_only, store, slug)


def _saved_only(store: Listings, slug: str, repo: str) -> Pulls:
    """Return the saved listing of ``repo``, or none held when nothing is saved yet or the save records a failure.

    Never lists live: the claim view runs on a budget a live GitHub listing can outlast, so a cold or failed cache
    admits the task instead of blocking the pass. The dedicated fleet red notice step owns the live listing and its
    save.
    """
    saved = store.redis.get(store.key(slug, f"fleet-red-pulls:{repo}"))
    return (_decoded(saved) or ()) if saved is not None else ()


def saver(store: Listings) -> Callable[[Write], None]:
    """Return the listings applier: it saves a listing for ten minutes only while no listing is saved under its key."""
    return partial(_save, store)


def _save(store: Listings, write: Write) -> None:
    """Save the listing of ``write`` unless another pass saved one since the step read none.

    A Redis timeout drops the write as a conflict, so the next pass lists and saves it again.
    """
    from redis.exceptions import TimeoutError as RedisTimeoutError

    try:
        saved = store.redis.set(write.key, str(write.value), ex=TTL, nx=True)
    except RedisTimeoutError as exc:
        raise RevisionConflict(f"Redis timed out saving the listing under {write.key}") from exc
    if not saved:
        raise RevisionConflict(f"another pass saved the listing under {write.key} since the step read none")


def _unsaved(reader: Reader, repo: str) -> Listed:
    """Return the reader's listing of ``repo`` with nothing to save."""
    return reader(repo), ()


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
                said = " ".join(str(getattr(exc, "stderr", None) or "").split())
                why = f"{exc}: {said}" if said else str(exc)
                print(f"fleet red hold skipped, listing open pull requests failed: {why}", file=sys.stderr)
                known[repo] = []
        return not held(known[repo], url, int(clock() * 1000))

    return admits


def holds(
    slug: str,
    store: RedisStore,
    rows: dict[str, dict[str, Any]],
    now_ms: int,
    reader: Reader | None = None,
) -> tuple[list[tuple[str, str, str]], list[str], list[Write]]:
    """Return the task, pull request and red checks of each unclaimed open task newly held, the lifted hold keys and
    the listing saves.

    A task whose hold was already noticed is left out, so the pass posts nothing for it. Nothing is posted, forgotten
    or saved.
    """
    unclaimed = [
        task["id"]
        for task in rows.values()
        if task.get("state") == "open" and task.get("pr_url") and not task.get("claimed_by")
    ]
    waiting = set(unclaimed) - store.claimed(slug, unclaimed)
    cleared = [store.key(slug, f"fleet-red:{task['id']}") for task in rows.values() if task["id"] not in waiting]
    fetch: Callable[[str], Listed] = partial(_unsaved, reader) if reader else partial(cached, store, slug)
    listings, saves = _listed(watched(rows), fetch)
    found: list[tuple[str, str, str]] = []
    for task_id in [task_id for task_id in unclaimed if task_id in waiting]:
        url, key = rows[task_id]["pr_url"], store.key(slug, f"fleet-red:{task_id}")
        pulls = listings[repository(url)]
        if pulls is None:
            continue
        if not held(pulls, url, now_ms):
            cleared.append(key)
            continue
        if not store.redis.exists(key):
            found.append((task_id, url, ", ".join(sorted(own(pulls, url)))))
    return found, cleared, saves


def watched(rows: dict[str, dict[str, Any]]) -> list[str]:
    """Return the repository of every task carrying a pull request that is not done, in row order.

    A claimed or in review task can reopen and be offered at once, so its repository's listing is kept saved for the
    claim view to read before that happens.
    """
    urls = [task["pr_url"] for task in rows.values() if task.get("pr_url") and task.get("state") != "done"]
    return list(dict.fromkeys(repository(url) for url in urls))


def _listed(repos: list[str], fetch: Callable[[str], Listed]) -> tuple[dict[str, Pulls | None], list[Write]]:
    """Return the listing of each repository, none for a failed read, and the saves of the fresh listings."""
    listings: dict[str, Pulls | None] = {}
    saves: list[Write] = []
    for repo in repos:
        try:
            listings[repo], listed = fetch(repo) if repo else ([], ())
        except ERRORS:
            listings[repo], listed = None, ()
        saves.extend(listed)
    return listings, saves


def post(slug: str, store: RedisStore, ledger: Any, task_id: str, url: str, checks: str) -> str:
    """Post the hold notice of ``task_id`` once, returning its action, or nothing when it was already posted."""
    if not store.redis.set(store.key(slug, f"fleet-red:{task_id}"), url, nx=True):
        return ""
    ledger.notice(slug, task_id, NOTICE.format(url=url, checks=checks))
    return held_action(task_id, url, checks)


def held_action(task_id: str, url: str, checks: str) -> str:
    """Return the action line naming the hold fleet red puts on ``task_id``."""
    return f"task {task_id} held unclaimed, {url} is red only from {checks}"
