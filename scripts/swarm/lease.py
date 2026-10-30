import os
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import TypeVar

from scripts.swarm import fencing as document
from scripts.swarm.store import RedisStore, SwarmError

TICK_MS = 60_000
TICK_REFUSAL = "AGENTIHOOKS_CONTROLLER_TICK_SECONDS must be at least 1 second"
STALE = "the controller lease is stale"
EPOCH = ContextVar("controller_epoch", default=None)
T = TypeVar("T")


def tick_ms() -> int:
    seconds = os.environ.get("AGENTIHOOKS_CONTROLLER_TICK_SECONDS")
    if not seconds:
        return TICK_MS
    try:
        ms = round(float(seconds) * 1000)
    except (ValueError, OverflowError) as exc:
        raise SwarmError(TICK_REFUSAL) from exc
    if ms < 1000:
        raise SwarmError(TICK_REFUSAL)
    return ms


def ttl_ms() -> int:
    return 3 * tick_ms()


def beat_s() -> float:
    return tick_ms() / 1000


@dataclass(frozen=True)
class Lease:
    owner: str
    epoch: int
    expires_at: int


def now_ms(store: RedisStore) -> int:
    return document.now_ms()


def _address(slug):
    from scripts.swarm_ledger.ledger_route import url

    return url(slug)


def _remote(slug, action, fields=None):
    import urllib.error

    from scripts.swarm.ledger_client import _ledger
    from scripts.swarm_ledger.api.client import ResourceClient, failure

    ledger = _ledger()
    payload = None if action == "current" else {"action": action, **fields}
    path = "controller-lease" if payload is None else "controller-lease/actions"
    try:
        return ResourceClient(_address(slug), ledger.credentials(slug, service=payload is not None)).request(
            slug, path, payload
        )["data"]
    except urllib.error.HTTPError as exc:
        replay, error = failure(exc)
        raise SwarmError(
            f"hosted controller lease refused: {replay.code} {error.get('code', '')} {error.get('message', '')}"
        ) from None
    except OSError as exc:
        raise SwarmError(f"hosted controller lease is unavailable: {exc}") from exc


def _recorded(doc: dict) -> Lease | None:
    return Lease(**doc["lease"]) if doc["lease"] else None


def _live(doc: dict, at: int) -> Lease | None:
    held = _recorded(doc)
    return held if held and held.expires_at > at else None


def _hold(doc: dict, held: Lease) -> Lease:
    doc["lease"] = asdict(held)
    doc["epoch"] = held.epoch
    doc["legacy_owner"] = ""
    return held


def current(store: RedisStore, slug: str) -> Lease | None:
    if _address(slug):
        data = _remote(slug, "current")
        return Lease(**data) if data is not None else None
    return _live(document.read(store, slug), now_ms(store))


def acquire(store: RedisStore, slug: str, owner: str) -> Lease | None:
    if _address(slug):
        data = _remote(slug, "acquire", {"owner": owner})
        return Lease(**data) if data is not None else None
    from scripts.swarm.commands import hive_id

    host = store.redis.get(store.key(slug, "controller-host"))
    if host and host != owner:
        return None
    hostname = socket.gethostname() if owner == hive_id() else owner

    def take(doc):
        at, held, legacy = now_ms(store), _recorded(doc), doc["legacy_owner"]
        if held and held.expires_at > at:
            if held.owner not in (owner, hostname):
                return None
            epoch, keeper = held.epoch, owner
        else:
            epoch = doc["epoch"] + 1
            keeper = legacy if legacy and held is None and legacy != hostname else owner
            doc["leader_changes"] += 1
        renewed = _hold(doc, Lease(keeper, epoch, at + ttl_ms()))
        return renewed if keeper == owner else None

    return document.write(store, slug, take)


def transfer(store: RedisStore, slug: str, owner: str, source_epoch: int, expected_epoch: int) -> Lease:
    if _address(slug):
        return Lease(
            **_remote(
                slug, "transfer", {"owner": owner, "source_epoch": source_epoch, "expected_epoch": expected_epoch}
            )
        )

    def move(doc):
        at = now_ms(store)
        if doc["legacy_owner"]:
            raise SwarmError("the controller lease belongs to an incompatible owner")
        live = _live(doc, at)
        if (live.epoch if live else 0) != expected_epoch or (live and live.owner != owner):
            raise SwarmError(STALE)
        epoch = max(doc["epoch"], source_epoch, live.epoch if live else 0) + 1
        return _hold(doc, Lease(owner, epoch, at + ttl_ms()))

    return document.write(store, slug, move)


def _check(doc: dict, at: int, held: Lease | int) -> None:
    live = _live(doc, at)
    if isinstance(held, Lease):
        if live is None or (live.owner, live.epoch) != (held.owner, held.epoch):
            raise SwarmError(STALE)
    elif live is None or live.epoch != held:
        raise SwarmError(STALE)


def fenced(store: RedisStore, slug: str, held: Lease | int | None, change: Callable[[dict], T]) -> T:
    """Commits change only while held, a lease or its epoch, is the live controller lease; None writes unfenced."""
    if held is None:
        return document.write(store, slug, change)
    if _address(slug):
        _check({"lease": _remote(slug, "current")}, now_ms(store), held)
        return document.write(store, slug, change)

    def guarded(doc):
        _check(doc, now_ms(store), held)
        return change(doc)

    return document.write(store, slug, guarded)


def require(store: RedisStore, slug: str, held: Lease) -> None:
    live = current(store, slug)
    if live is None or (live.owner, live.epoch) != (held.owner, held.epoch):
        raise SwarmError(STALE)


def renew(store: RedisStore, slug: str, held: Lease) -> Lease:
    if _address(slug):
        return Lease(**_remote(slug, "renew", {"owner": held.owner, "epoch": held.epoch}))

    def extend(doc):
        at = now_ms(store)
        _check(doc, at, held)
        live = _recorded(doc)
        if live.expires_at - at >= ttl_ms() - tick_ms():
            return live
        doc["lease"] = asdict(Lease(held.owner, held.epoch, at + ttl_ms()))
        return Lease(**doc["lease"])

    return document.write(store, slug, extend)


def beat(store: RedisStore, slug: str, held: Lease, stop: threading.Event) -> None:
    while not stop.is_set():
        if stop.wait(beat_s()):
            return
        try:
            renew(store, slug, held)
        except SwarmError:
            return
        except document.unavailable():
            continue


@contextmanager
def kept(store: RedisStore, slug: str, held: Lease) -> Iterator[None]:
    stop = threading.Event()
    from contextvars import copy_context

    beating = threading.Thread(target=copy_context().run, args=(beat, store, slug, held, stop))
    beating.start()
    try:
        yield
    finally:
        stop.set()
        beating.join()


def require_epoch(store: RedisStore, slug: str, epoch: int) -> None:
    live = current(store, slug)
    if live is None or live.epoch != epoch:
        raise SwarmError(STALE)


@contextmanager
def fencing(epoch: int | None) -> Iterator[None]:
    token = EPOCH.set(epoch)
    try:
        yield
    finally:
        EPOCH.reset(token)


def release(store: RedisStore, slug: str, held: Lease) -> bool:
    if _address(slug):
        return _remote(slug, "release", {"owner": held.owner, "epoch": held.epoch})

    def drop(doc):
        _check(doc, now_ms(store), held)
        doc["lease"] = None
        return True

    try:
        return document.write(store, slug, drop)
    except SwarmError:
        return False
