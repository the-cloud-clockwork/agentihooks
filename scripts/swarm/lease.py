import json
import time
from dataclasses import asdict, dataclass

from redis.exceptions import WatchError

from scripts.swarm.store import RedisStore, SwarmError

TICK_MS = 60_000
TTL_MS = 3 * TICK_MS


@dataclass(frozen=True)
class Lease:
    owner: str
    epoch: int
    expires_at: int


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def current(store: RedisStore, slug: str) -> Lease | None:
    raw = store.redis.get(store.key(slug, "control-owner"))
    held = Lease(**json.loads(raw)) if raw and raw.startswith("{") else None
    return held if held and held.expires_at > now_ms() else None


def acquire(store: RedisStore, slug: str, owner: str) -> Lease | None:
    key, epochs = store.key(slug, "control-owner"), store.key(slug, "control-epoch")
    while True:
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(key, epochs)
                raw, at = pipe.get(key), now_ms()
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
                pipe.execute()
                return renewed if keeper == owner else None
            except WatchError:
                continue


def require(store: RedisStore, slug: str, held: Lease) -> None:
    live = current(store, slug)
    if live is None or (live.owner, live.epoch) != (held.owner, held.epoch):
        raise SwarmError("the controller lease is stale")


def release(store: RedisStore, slug: str, held: Lease) -> bool:
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
