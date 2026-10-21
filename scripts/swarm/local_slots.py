from scripts.swarm import lease
from scripts.swarm.store import PREFIX, RedisStore
from scripts.swarm_v2.runtime.base import LOCAL

PENDING_MS = 300000


def key(machine: str) -> str:
    return f"{PREFIX}:local-slots:{machine}"


def held(store: RedisStore, machine: str, slug: str, task: str) -> int:
    return len(_held(store, store.redis.hgetall(key(machine)), f"{slug}/{task}", lease.now_ms(store))[0])


def claim(store: RedisStore, machine: str, slug: str, task: str, local_max: int) -> int:
    from redis.exceptions import WatchError

    own = f"{slug}/{task}"
    while True:
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(key(machine))
                now = lease.now_ms(store)
                taken, settled = _held(store, pipe.hgetall(key(machine)), own, now)
                if len(taken) >= local_max:
                    return len(taken)
                pipe.multi()
                if settled:
                    pipe.hdel(key(machine), *settled)
                pipe.hset(key(machine), own, now)
                pipe.execute()
                return len(taken)
            except WatchError:
                continue


def release(store: RedisStore, machine: str, slug: str, task: str) -> None:
    store.redis.hdel(key(machine), f"{slug}/{task}")


def _held(store: RedisStore, claims: dict, own: str, now: int) -> tuple[list[str], list[str]]:
    agents = [f"{slug}/{agent.task}" for slug in store.slugs() for agent in store.agents(slug) if _local_worker(agent)]
    pending = [claimed for claimed, at in claims.items() if claimed not in agents and now - int(at) < PENDING_MS]
    settled = [claimed for claimed in claims if claimed not in pending]
    return [claimed for claimed in (*agents, *pending) if claimed != own], settled


def _local_worker(agent) -> bool:
    return agent.runtime_backend == LOCAL and not agent.hive and agent.lane != "master" and agent.state != "finished"
