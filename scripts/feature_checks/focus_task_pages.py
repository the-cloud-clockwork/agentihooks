"""Live check of the paged focus read: in every swarm that is not stopped, the task list of a focus target read in
pages at ``focus/<target>/tasks`` holds the task rows the one reply read of ``focus/<target>`` holds; a target one
reply cannot hold is held instead to the list the ledger hashes, through a cursor made from the hash of its pages.
It checks the focus records with the most tasks of each ledger, or the first three the freezes read lists when the
focus list itself passes the reply limit, prints one line per target and never a credential."""

import sys
import urllib.error
from collections.abc import Callable, Iterable
from typing import Any, Protocol
from urllib.parse import urlencode

from scripts.swarm_ledger.api import resources

FAIL = "FAIL: "
SAME = "{slug} focus {target}: {count} task rows in pages, as the read of the target holds"
PAGED = "{slug} focus {target}: {count} task rows in pages, more than one reply holds"
DIFFERENT = "{slug} focus {target}: the pages hold {paged} task rows that differ from the {whole} the target holds"
BROKEN = "{slug} focus {target}: the pages hold {paged} task rows that are not the list the ledger holds"
NONE = "{slug} holds no focus record"
CHECKED = 3
ATTEMPTS = 3
KEYS = ("id", "lane", "state", "verdict")
TOO_LARGE = 413
CONFLICT = 409
FOCUS = "focus"

Read = Callable[[str, str, bool], Any]


class Swarms(Protocol):
    """What the check reads of the swarm store: the swarms and the state of each."""

    def slugs(self) -> Iterable[str]: ...

    def config(self, slug: str) -> Any: ...


def lines(slug: str, read: Read) -> list[str]:
    """One line per checked focus target of a ledger, a failing one where the pages and the read of the target, or
    the list the ledger hashes, still differ after every attempt, or one line that says the ledger holds no focus
    record."""
    return [_target(slug, target, read) for target in _targets(slug, read)] or [NONE.format(slug=slug)]


def _targets(slug: str, read: Read) -> list[str]:
    """The targets to check: the focus records with the most tasks. A focus list one reply cannot hold names no
    size, so its first records are taken as the freezes read lists them instead; any other refusal is raised."""
    try:
        records: list[dict[str, Any]] = read(slug, "focus", True)
    except urllib.error.HTTPError as refused:
        if refused.code != TOO_LARGE:
            raise
        listed: list[dict[str, Any]] = read(slug, "freezes", True)
        return [record["target"] for record in listed if record["verb"] == FOCUS][:CHECKED]
    return [record["target"] for record in sorted(records, key=_size, reverse=True)[:CHECKED]]


def _size(record: dict[str, Any]) -> int:
    return len(record["tasks"])


def _rows(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [tuple(row.get(key) for key in KEYS) for row in rows]


def _bare(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if key != "revision"} for row in rows]


def _target(slug: str, target: str, read: Read, attempts: int = ATTEMPTS) -> str:
    """The line of one target, judged once and read again with one attempt less while it does not hold; the failing
    line is written when no attempt is left. The pages are read first and the one reply right after them, so the
    ledger has the time of one read to move between the two. A target one reply cannot hold has only its pages,
    held to the list the ledger hashes; any other refusal is raised."""
    rows = read(slug, f"focus/{target}/tasks", True)
    try:
        whole = _rows(read(slug, f"focus/{target}", False)["tasks"])
    except urllib.error.HTTPError as refused:
        if refused.code != TOO_LARGE:
            raise
        if _hashed(slug, target, read, rows):
            return PAGED.format(slug=slug, target=target, count=len(rows))
        failing = BROKEN.format(slug=slug, target=target, paged=len(rows))
    else:
        if whole == _rows(rows):
            return SAME.format(slug=slug, target=target, count=len(rows))
        failing = DIFFERENT.format(slug=slug, target=target, paged=len(rows), whole=len(whole))
    if attempts > 1:
        return _target(slug, target, read, attempts - 1)
    return FAIL + failing


def _hashed(slug: str, target: str, read: Read, rows: list[dict[str, Any]]) -> bool:
    """Whether the pages of a target are the list the ledger hashes: one row asked at the cursor made from the hash
    of their bare rows is answered, and refused as a conflict when the ledger holds another list. Any other refusal
    is raised."""
    cursor = f"{resources.revision(_bare(rows))}:0"
    try:
        read(slug, f"focus/{target}/tasks?" + urlencode({"limit": 1, "cursor": cursor}), False)
    except urllib.error.HTTPError as refused:
        if refused.code != CONFLICT:
            raise
        return False
    return True


def _running(store: Swarms) -> list[str]:
    """The swarms that are not stopped, in name order."""
    return [slug for slug in sorted(store.slugs()) if store.config(slug).state != "stopped"]


def main(store: Swarms | None = None, read: Read | None = None) -> int:
    if store is None:
        from scripts.swarm.store import RedisStore, redis_client  # pyright: ignore[reportUnknownVariableType]

        store = RedisStore(redis_client())
    if read is None:
        from scripts.swarm_ledger import ledger

        read = ledger.resource
    found = [line for slug in _running(store) for line in lines(slug, read)]
    for line in found:
        print(line)
    return int(any(line.startswith(FAIL) for line in found))


if __name__ == "__main__":
    sys.exit(main())
