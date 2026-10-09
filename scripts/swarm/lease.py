import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass

from scripts.swarm.store import RedisStore, SwarmError

TICK_MS = 60_000
TTL_MS = 3 * TICK_MS
EPOCH = ContextVar("controller_epoch", default=None)


@dataclass(frozen=True)
class Lease:
    owner: str
    epoch: int
    expires_at: int


def now_ms(store: RedisStore) -> int:
    seconds, microseconds = store.redis.time()
    return seconds * 1000 + microseconds // 1000


def current(store: RedisStore, slug: str) -> Lease | None:
    raw = store.redis.get(store.key(slug, "control-owner"))
    held = Lease(**json.loads(raw)) if raw and raw.startswith("{") else None
    return held if held and held.expires_at > now_ms(store) else None


def acquire(store: RedisStore, slug: str, owner: str) -> Lease | None:
    from redis.exceptions import WatchError

    key, epochs = store.key(slug, "control-owner"), store.key(slug, "control-epoch")
    while True:
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(key, epochs)
                raw, at = pipe.get(key), now_ms(store)
                held = Lease(**json.loads(raw)) if raw and raw.startswith("{") else None
                if held and held.expires_at > at:
                    if held.owner != owner:
                        return None
                    epoch, keeper = held.epoch, owner
                else:
                    epoch = int(pipe.get(epochs) or 0) + 1
                    keeper = raw if raw and held is None else owner
                renewed = Lease(keeper, epoch, at + TTL_MS)
                pipe.multi()
                pipe.set(key, json.dumps(asdict(renewed)), px=TTL_MS)
                pipe.set(epochs, epoch)
                if held is None or held.expires_at <= at:
                    pipe.incr(store.key(slug, "controller-leader-changes"))
                pipe.execute()
                return renewed if keeper == owner else None
            except WatchError:
                continue


def renew(store: RedisStore, slug: str, held: Lease) -> Lease | None:
    from redis.exceptions import WatchError

    key = store.key(slug, "control-owner")
    for _ in range(8):
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(key)
                raw, at = pipe.get(key), now_ms(store)
                live = Lease(**json.loads(raw)) if raw and raw.startswith("{") else None
                if live is None or live.expires_at <= at or (live.owner, live.epoch) != (held.owner, held.epoch):
                    return None
                renewed = Lease(held.owner, held.epoch, at + TTL_MS)
                pipe.multi()
                pipe.set(key, json.dumps(asdict(renewed)), px=TTL_MS)
                pipe.execute()
                return renewed
            except WatchError:
                continue
    raise SwarmError("controller lease kept changing; renewal was not committed")


def require(store: RedisStore, slug: str, held: Lease) -> None:
    live = current(store, slug)
    if live is None or (live.owner, live.epoch) != (held.owner, held.epoch):
        raise SwarmError("the controller lease is stale")


def renew(store: RedisStore, slug: str, held: Lease) -> Lease:
    from redis.exceptions import WatchError

    key = store.key(slug, "control-owner")
    while True:
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(key)
                require(store, slug, held)
                renewed = Lease(held.owner, held.epoch, now_ms(store) + TTL_MS)
                pipe.multi()
                pipe.set(key, json.dumps(asdict(renewed)), px=TTL_MS)
                pipe.execute()
                return renewed
            except WatchError:
                continue


def require_epoch(store: RedisStore, slug: str, epoch: int) -> None:
    live = current(store, slug)
    if live is None or live.epoch != epoch:
        raise SwarmError("the controller lease is stale")


@contextmanager
def fencing(epoch: int) -> Iterator[None]:
    token = EPOCH.set(epoch)
    try:
        yield
    finally:
        EPOCH.reset(token)


def release(store: RedisStore, slug: str, held: Lease) -> bool:
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
