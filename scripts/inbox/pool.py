"""The master inbox pool of a swarm with two or more live masters: items for the front door seat wait here in arrival
order, and each is claimed by exactly one live master, in round robin seat order. Operator items never enter it."""

from scripts.inbox import intent
from scripts.inbox.seats import is_seat, master_of
from scripts.inbox.store import POOL_HOLDER, POOL_SKIP
from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import MASTER, RedisStore
from scripts.swarm_v2 import masters

PREFIX = f"{ROOT}:inbox:pool"
POOLS = f"{PREFIX}s"
OPERATOR = "operator"
CLAIMER = "inbox"
CLAIM_ATTEMPTS = 3
DROPPED = object()


def slug_of(address: str) -> str:
    """The swarm whose front door master seat address is, '' for any other address."""
    seat, _, slug = address.partition("@")
    return slug if is_seat(address) and seat == MASTER else ""


def number(address: str) -> int:
    seat = address.partition("@")[0]
    return 1 if seat == MASTER else int(seat.removeprefix(f"{MASTER}-"))


def live_masters(redis, slug: str, agents: list | None = None) -> list[str]:
    agents = RedisStore(redis).agents(slug) if agents is None else agents
    waiting = {agent.name for agent in agents if agent.input_prompt}
    return [seat for seat, name in masters.held(redis, slug, agents).items() if is_seat(seat) and name not in waiting]


def turn(live: list[str], last: str, skip: str | None) -> str:
    """The live master after last in seat order, passing over skip while another master is live."""
    choices = [seat for seat in live if seat != skip] or live
    later = [seat for seat in choices if last and number(seat) > number(last)]
    return (later or choices)[0]


class MasterPool:
    def __init__(self, inbox) -> None:
        self.inbox = inbox
        self.redis = inbox.redis

    def key(self, slug: str, *parts: str) -> str:
        return self.inbox.key("pool", slug, *parts)

    def masters_for(self, sender: str, address: str) -> list[str]:
        """The live masters an item from sender to address is pooled across; empty sends it as before, as it does when
        a live master's inbox sits in another hive member's keys."""
        slug = slug_of(address)
        if not slug or sender == OPERATOR:
            return []
        live = live_masters(self.redis, slug)
        if any(self.inbox.home(seat) != self.inbox.world for seat in live):
            return []
        return live if len(live) > 1 or (live and address not in live) else []

    def stage_add(self, pipe, item) -> None:
        """pipe is in MULTI; the score is the item's place in its front door's arrival order."""
        slug = slug_of(item.address)
        pipe.zadd(self.key(slug), {item.id: item.sequence})
        pipe.sadd(self.inbox.key("pools"), slug)

    def stage_remove(self, pipe, item) -> None:
        if slug := slug_of(item.address):
            pipe.zrem(self.key(slug), item.id)

    def holds(self, pipe, item) -> bool:
        slug = slug_of(item.address)
        if not slug:
            return False
        pipe.watch(self.key(slug))
        return pipe.zscore(self.key(slug), item.id) is not None

    def waiting(self, pipe, address: str) -> set[str]:
        """The ids waiting in the pool of the front door seat address; empty for any other address."""
        slug = slug_of(address)
        if not slug:
            return set()
        pipe.watch(self.key(slug))
        return set(pipe.zrange(self.key(slug), 0, -1))

    def takes_back(self, holder: str | None) -> bool:
        """An unconfirmed item returns to the pool while another master is live or its holder is gone."""
        if not holder:
            return False
        live = live_masters(self.redis, slug_of(master_of(holder)))
        return len(live) > 1 or holder not in live

    def reclaimable(self, pipe, holder: str) -> bool:
        self.inbox.seats.watch(pipe, holder)
        return holder not in live_masters(self.redis, slug_of(master_of(holder)))

    def keys_of(self, address: str) -> list[str]:
        slug = slug_of(address)
        keys = (self.key(slug), self.key(slug, "last"), self.key(slug, "holders"))
        return [key for key in keys if slug and self.redis.exists(key)]

    def distribute_all(self) -> list:
        return [item for slug in sorted(self.redis.smembers(self.inbox.key("pools"))) for item in self.distribute(slug)]

    def distribute(self, slug: str) -> list:
        """Hand the items waiting when the pass began, oldest first, to the next live master; stop early when another
        process is claiming from the pool, since that process carries on. A later item waits for its own sender's
        pass or the next redelivery."""
        live = live_masters(self.redis, slug)
        self._reclaim(slug, live)
        routed = self._routes(slug, live)
        claimed = []
        for _ in range(self.redis.zcard(self.key(slug)) + 1):
            item = self._claim_next(slug, live, routed)
            if item is None:
                break
            if item is not DROPPED:
                claimed.append(item)
        return claimed

    def _reclaim(self, slug, live):
        """Unsettled items a master that is gone claimed go back to the pool."""
        for seat in sorted(self.redis.smembers(self.key(slug, "holders")) - set(live)):
            retry = False
            for item in self.inbox.open_items(seat):
                moved = self.inbox.return_to_pool(item.id, seat)
                if moved is None and self.redis.hget(self.inbox.key("item", item.id), POOL_HOLDER) == seat:
                    retry = True
            if not retry:
                self.redis.srem(self.key(slug, "holders"), seat)

    def _routes(self, slug, live):
        """None with intent grouping off; else the matched master per waiting item, None leaving it to round robin."""
        if not intent.enabled():
            return None
        items = [self.inbox.get(item_id) for item_id, _ in self.redis.zscan_iter(self.key(slug))]
        return intent.routes(self, slug, items, live)

    def _claim_next(self, slug, live, routed):
        from redis.exceptions import WatchError

        for _ in range(CLAIM_ATTEMPTS):
            try:
                return self._try_claim(slug, live, routed)
            except WatchError:
                continue
        return None

    def _try_claim(self, slug, live, routed):
        pool, last = self.key(slug), self.key(slug, "last")
        with self.redis.pipeline() as pipe:
            pipe.watch(pool, last)
            head = pipe.zrange(pool, 0, 0)
            if not head:
                if not pipe.exists(self.key(slug, "holders")):
                    pipe.multi()
                    pipe.srem(self.inbox.key("pools"), slug)
                    pipe.execute()
                return None
            if not live:
                return None
            item_key = self.inbox.key("item", head[0])
            pipe.watch(item_key)
            if pipe.hget(item_key, "state") != "pending":
                pipe.multi()
                pipe.zrem(pool, head[0])
                pipe.execute()
                return DROPPED
            item = self.inbox.get(head[0])
            skip = pipe.hget(item_key, POOL_SKIP)
            seat = (routed or {}).get(item.id)
            if seat not in live or seat == skip:
                seat = turn(live, pipe.get(last) or "", skip)
            pipe.multi()
            pipe.zrem(pool, item.id)
            pipe.set(last, seat)
            pipe.sadd(self.key(slug, "holders"), seat)
            if routed is not None:
                intent.remember(pipe, self, slug, seat, item)
            moved = self.inbox.stage_claim(pipe, item, seat, CLAIMER)
            pipe.execute()
            return moved
