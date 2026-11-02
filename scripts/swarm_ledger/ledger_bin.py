"""The HOME bin: ledgers hidden from HOME, kept 30 days, then deleted for good.

A small ledger moves itself here once every item in it is done or after 7 days with no change.
"""

import threading
from collections.abc import Callable
from typing import Any, cast

import ledger_core as core

KEEP_DAYS = 30
IDLE_DAYS = 7
DAY_MS = 24 * 60 * 60 * 1000
LOCK = threading.Lock()


def restored() -> dict[str, int]:
    from scripts.swarm_ledger.repository import bin_storage

    return cast("dict[str, int]", bin_storage.restored())


def entries() -> dict[str, int]:
    from scripts.swarm_ledger.repository import bin_storage

    return cast("dict[str, int]", bin_storage.entries())


def days_left(deleted_at: int, now: int) -> int:
    return max(0, -((now - deleted_at - KEEP_DAYS * DAY_MS) // DAY_MS))


def delete(slug: str, now: int | None = None) -> None:
    from scripts.swarm_ledger import storage

    repository = storage.default()

    return repository.delete(slug, now)


def require_local(slug: str) -> None:
    from scripts.swarm_ledger import ledger_link

    remote = cast("Callable[..., bool]", ledger_link.remote)
    if remote():
        raise SystemExit(f"remote ledger clients must use /api/v1/ledgers/{slug}/lifecycle/actions")


def restore(slug: str, now: int | None = None) -> bool:
    require_local(slug)
    from scripts.swarm_ledger import storage

    repository = storage.default()

    return repository.restore(slug, now)


def bin_closed(slug: str, closed_at: int, now: int | None = None) -> bool:
    require_local(slug)
    from scripts.swarm_ledger.repository import bin_storage

    fn = cast("Callable[..., bool]", bin_storage.bin_closed)
    return fn(slug, closed_at, now)


def purge_expired(now: int | None = None) -> list[str]:
    from scripts.swarm_ledger.repository import bin_storage

    fn = cast("Callable[..., list[str]]", bin_storage.purge_expired)
    return fn(now)


def _answered(question: dict[str, Any]) -> bool:
    return any(not a.get("deleted") and a.get("text", "").strip() for a in question.get("answers", []))


def finished(doc: dict[str, Any]) -> bool:
    items = [i for key in ("phases", "followups", "tasks") for i in doc.get(key, []) if not i.get("out_of_scope")]
    questions = [q for q in doc.get("questions", []) if not q.get("out_of_scope")]
    return (
        bool(items)
        and all(i.get("done") is True or i.get("state") == "done" for i in items)
        and all(_answered(q) for q in questions)
    )


def idle_due(done: bool, changed: int, now: int, restored_at: int | None = None) -> bool:
    if restored_at is not None and changed <= restored_at:
        return now - restored_at > IDLE_DAYS * DAY_MS
    return done or now - changed > IDLE_DAYS * DAY_MS


def auto_bin(now: int | None = None) -> list[str]:
    from scripts.swarm_ledger.repository import bin_storage

    fn = cast("Callable[..., list[str]]", bin_storage.auto_bin)
    return fn(now)


def tidy(now: int | None = None) -> tuple[list[str], list[str]]:
    now = core.now_ms() if now is None else now
    return auto_bin(now), purge_expired(now)
