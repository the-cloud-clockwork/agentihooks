import json
import os
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass

from scripts.swarm.store import RedisStore, SwarmError

TICK_MS = 60_000
TICK_REFUSAL = "AGENTIHOOKS_CONTROLLER_TICK_SECONDS must be at least 1 second"
EPOCH = ContextVar("controller_epoch", default=None)


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
    seconds, microseconds = store.redis.time()
    return seconds * 1000 + microseconds // 1000


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


def current(store: RedisStore, slug: str) -> Lease | None:
    if _address(slug):
        data = _remote(slug, "current")
        return Lease(**data) if data is not None else None
    raw = store.redis.get(store.key(slug, "control-owner"))
    held = Lease(**json.loads(raw)) if raw and raw.startswith("{") else None
    return held if held and held.expires_at > now_ms(store) else None


def acquire(store: RedisStore, slug: str, owner: str) -> Lease | None:
    if _address(slug):
        data = _remote(slug, "acquire", {"owner": owner})
        return Lease(**data) if data is not None else None
    from redis.exceptions import WatchError

    from scripts.swarm.commands import hive_id

    hostname = socket.gethostname() if owner == hive_id() else owner
    key, epochs = store.key(slug, "control-owner"), store.key(slug, "control-epoch")
    while True:
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(key, epochs)
                raw, at = pipe.get(key), now_ms(store)
                held = Lease(**json.loads(raw)) if raw and raw.startswith("{") else None
                if held and held.expires_at > at:
                    if held.owner not in (owner, hostname):
                        return None
                    epoch, keeper = held.epoch, owner
                else:
                    epoch = int(pipe.get(epochs) or 0) + 1
                    keeper = raw if raw and held is None and raw != hostname else owner
                ttl = ttl_ms()
                renewed = Lease(keeper, epoch, at + ttl)
                pipe.multi()
                pipe.set(key, json.dumps(asdict(renewed)), px=ttl)
                pipe.set(epochs, epoch)
                if held is None or held.expires_at <= at:
                    pipe.incr(store.key(slug, "controller-leader-changes"))
                pipe.execute()
                return renewed if keeper == owner else None
            except WatchError:
                continue


def transfer(store: RedisStore, slug: str, owner: str, source_epoch: int, expected_epoch: int) -> Lease:
    from redis.exceptions import WatchError

    if _address(slug):
        return Lease(
            **_remote(
                slug, "transfer", {"owner": owner, "source_epoch": source_epoch, "expected_epoch": expected_epoch}
            )
        )
    key, epochs = store.key(slug, "control-owner"), store.key(slug, "control-epoch")
    for _ in range(5):
        at = now_ms(store)
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(key, epochs)
                raw = pipe.get(key)
                held = Lease(**json.loads(raw)) if raw and raw.startswith("{") else None
                if raw and held is None:
                    raise SwarmError("the controller lease belongs to an incompatible owner")
                live = held if held and held.expires_at > at else None
                if (live.epoch if live else 0) != expected_epoch or (live and live.owner != owner):
                    raise SwarmError("the controller lease is stale")
                epoch = max(int(pipe.get(epochs) or 0), source_epoch, live.epoch if live else 0) + 1
                ttl = ttl_ms()
                moved = Lease(owner, epoch, at + ttl)
                pipe.multi()
                pipe.set(key, json.dumps(asdict(moved)), px=ttl)
                pipe.set(epochs, epoch)
                pipe.execute()
                return moved
            except WatchError:
                continue
    raise SwarmError("the controller lease kept changing; transfer was not committed")


def require(store: RedisStore, slug: str, held: Lease) -> None:
    live = current(store, slug)
    if live is None or (live.owner, live.epoch) != (held.owner, held.epoch):
        raise SwarmError("the controller lease is stale")


def renew(store: RedisStore, slug: str, held: Lease) -> Lease:
    if _address(slug):
        return Lease(**_remote(slug, "renew", {"owner": held.owner, "epoch": held.epoch}))
    from redis.exceptions import WatchError

    key = store.key(slug, "control-owner")
    while True:
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(key)
                require(store, slug, held)
                ttl = ttl_ms()
                renewed = Lease(held.owner, held.epoch, now_ms(store) + ttl)
                pipe.multi()
                pipe.set(key, json.dumps(asdict(renewed)), px=ttl)
                pipe.execute()
                return renewed
            except WatchError:
                continue


def beat(store: RedisStore, slug: str, held: Lease, stop: threading.Event) -> None:
    from redis.exceptions import RedisError

    while not stop.is_set():
        if stop.wait(beat_s()):
            return
        try:
            renew(store, slug, held)
        except SwarmError:
            return
        except RedisError:
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
        raise SwarmError("the controller lease is stale")


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
    from redis.exceptions import WatchError

    key = store.key(slug, "control-owner")
    with store.redis.pipeline() as pipe:
        try:
            pipe.watch(key)
            require(store, slug, held)
            pipe.multi()
            pipe.delete(key)
            pipe.execute()
            return True
        except (SwarmError, WatchError):
            return False
