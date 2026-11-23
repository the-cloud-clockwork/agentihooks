"""Live check of the paged focus read: in every swarm that is not stopped, the task list of a focus target read in
pages at ``focus/<target>/tasks`` holds the task rows the one reply read of ``focus/<target>`` holds. It checks the
focus records with the most tasks of each ledger, prints one line per target and never a credential."""

import sys
from collections.abc import Callable, Iterable
from typing import Any, Protocol

FAIL = "FAIL: "
SAME = "{slug} focus {target}: {count} task rows in pages, as the read of the target holds"
DIFFERENT = "{slug} focus {target}: the pages hold {paged} task rows that differ from the {whole} the target holds"
NONE = "{slug} holds no focus record"
CHECKED = 3
ATTEMPTS = 3
KEYS = ("id", "lane", "state", "verdict")

Read = Callable[[str, str, bool], Any]


class Swarms(Protocol):
    """What the check reads of the swarm store: the swarms and the state of each."""

    def slugs(self) -> Iterable[str]: ...

    def config(self, slug: str) -> Any: ...


def lines(slug: str, read: Read) -> list[str]:
    """One line per checked focus target of a ledger, a failing one where the pages and the read of the target
    still differ after both were read again, or one line that says the ledger holds no focus record."""
    records: list[dict[str, Any]] = read(slug, "focus", True)
    largest = sorted(records, key=_size, reverse=True)[:CHECKED]
    return [_target(slug, record["target"], read) for record in largest] or [NONE.format(slug=slug)]


def _size(record: dict[str, Any]) -> int:
    return len(record["tasks"])


def _rows(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [tuple(row.get(key) for key in KEYS) for row in rows]


def _target(slug: str, target: str, read: Read) -> str:
    """The line of one target. A ledger that moves between the two reads is read again, so only a difference that
    stays is a failure."""
    whole: list[tuple[Any, ...]] = []
    paged: list[tuple[Any, ...]] = []
    for _ in range(ATTEMPTS):
        whole = _rows(read(slug, f"focus/{target}", False)["tasks"])
        paged = _rows(read(slug, f"focus/{target}/tasks", True))
        if whole == paged:
            return SAME.format(slug=slug, target=target, count=len(paged))
    return FAIL + DIFFERENT.format(slug=slug, target=target, paged=len(paged), whole=len(whole))


def main(store: Swarms | None = None, read: Read | None = None) -> int:
    if store is None:
        from scripts.swarm.store import RedisStore, redis_client  # pyright: ignore[reportUnknownVariableType]

        store = RedisStore(redis_client())
    if read is None:
        from scripts.swarm_ledger import ledger

        read = ledger.resource
    running = [slug for slug in sorted(store.slugs()) if store.config(slug).state != "stopped"]
    found = [line for slug in running for line in lines(slug, read)]
    for line in found:
        print(line)
    return int(any(line.startswith(FAIL) for line in found))


if __name__ == "__main__":
    sys.exit(main())
