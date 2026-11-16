import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

REMOTE_SERVICE = "a remote ledger client cannot make service writes; the operator credential stays on its host"
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


def service_refused(slug: str) -> bool:
    """Whether this process is a pinned agent on a remote host acting as the swarm's own service on ledger ``slug``
    without the tick's credential: the operator's service credential stays on its host."""
    from scripts.gates import Who
    from scripts.swarm_ledger.ledger_link import remote

    return Who.from_env().pinned and remote(slug=slug) and not tick_headers(slug)


def _hosted_secret(server_secret) -> str | None:
    from scripts.swarm_ledger.hosted_probe import HOSTED_ENV

    if not os.environ.get(HOSTED_ENV):
        return None
    try:
        return server_secret()
    except (KeyError, OSError, TypeError, ValueError):
        return None


def hosted() -> str | None:
    from scripts.swarm_ledger.storage_migration.hosted import server_secret

    return _hosted_secret(server_secret)


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
        yield store.slugs()


os.register_at_fork(after_in_child=_clear)
