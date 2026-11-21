"""The master launch lock of a swarm: one Redis key set if absent with a lease. Every master starter takes it before it
reads who holds the master seats and keeps it through the launch, so two starters in the same minute never both start
a master; a starter that dies mid launch frees it when the lease ends."""

import uuid
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from functools import partial
from typing import Any

KEY = "master-launch-lock"
LEASE_MS = 5 * 60 * 1000
HELD = "the master launch lock of swarm {slug} is held by {holder}, so no second master starts"
EXPIRED = "a starter whose lease just ended"


def key(store: Any, slug: str) -> str:
    """The Redis key of swarm ``slug``'s master launch lock."""
    return store.key(slug, KEY)


def held(store: Any, slug: str, starter: str) -> AbstractContextManager[str]:
    """Hold swarm ``slug``'s master launch lock for ``starter`` while the block runs. Yields an empty string when it
    took the lock, else the refusal naming the starter that holds it; releases only the lock this call took."""
    return holding(partial(take, store, slug, starter), partial(release, store, slug))


@contextmanager
def holding(take_lock: Callable[[], tuple[str, str]], release_lock: Callable[[str], None]) -> Generator[str]:
    """Hold the master launch lock ``take_lock`` takes while the block runs. Yields the refusal naming its holder,
    empty when it took the lock, and hands only the token it took to ``release_lock``."""
    token, refused = take_lock()
    if refused:
        yield refused
        return
    try:
        yield ""
    finally:
        release_lock(token)


def take(store: Any, slug: str, starter: str) -> tuple[str, str]:
    """Take swarm ``slug``'s master launch lock for ``starter``: its token and no refusal, else no token and the
    refusal naming the starter that holds it."""
    token = f"{starter} {uuid.uuid4().hex}"
    if store.redis.set(key(store, slug), token, nx=True, px=LEASE_MS):
        return token, ""
    found = store.redis.get(key(store, slug))
    return "", HELD.format(slug=slug, holder=found.rpartition(" ")[0] if found else EXPIRED)


def release(store: Any, slug: str, token: str) -> None:
    """Release swarm ``slug``'s master launch lock only while it still holds ``token``."""
    _release(store, key(store, slug), token)


def _release(store: Any, name: str, token: str) -> None:
    """Delete the lock at ``name`` only while it still holds ``token``."""
    from redis.exceptions import WatchError

    with store.redis.pipeline() as pipe:
        try:
            pipe.watch(name)
            if pipe.get(name) == token:
                pipe.multi()
                pipe.delete(name)
                pipe.execute()
        except WatchError:
            return
