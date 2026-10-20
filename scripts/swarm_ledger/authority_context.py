import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

_current = None


def _clear() -> None:
    global _current
    _current = None


def secret() -> str | None:
    return _current or os.environ.get("SWARM_SECRET")


def tick_headers(slug: str) -> dict[str, str]:
    from scripts.swarm_ledger.ledger_authority import TICK_TTL, tick_token

    if not _current:
        return {}
    return {"X-Ledger-Tick": tick_token(_current, slug, int(time.time()) + TICK_TTL)}


def _hosted_secret(server_secret) -> str | None:
    from scripts.swarm_ledger.ledger_link import HOSTED_ENV

    if not os.environ.get(HOSTED_ENV):
        return None
    try:
        return server_secret()
    except (KeyError, OSError, TypeError, ValueError):
        return None


@contextmanager
def bound(value: str | None) -> Iterator[None]:
    global _current
    previous = _current
    os.environ.pop("SWARM_SECRET", None)
    _current = value
    try:
        yield
    finally:
        _current = previous


@contextmanager
def for_tick(store: object) -> Iterator[list[str]]:
    from scripts.swarm_ledger.storage_migration.hosted import server_secret

    value = server_secret() if os.environ.get("SWARM_URL") else _hosted_secret(server_secret)
    with bound(value):
        slugs = store.slugs()
        if not value and any(store.config(slug).api_url or store.config(slug).pending_api_url for slug in slugs):
            value = server_secret()
        with bound(value):
            yield slugs


os.register_at_fork(after_in_child=_clear)
