from scripts.swarm import lease
from scripts.swarm.store import PREFIX, RedisStore
from scripts.swarm_v2.runtime.base import LOCAL

PENDING_MS = 300000
STARTING = "starting"


def key(machine: str) -> str:
    return f"{PREFIX}:local-slots:{machine}"


def held(store: RedisStore, machine: str, slug: str, task: str) -> int:
    return len(_held(store, store.redis.hgetall(key(machine)), f"{slug}/{task}", lease.now_ms(store))[0])


def claim(store: RedisStore, machine: str, slug: str, task: str, name: str, local_max: int) -> int:
    from redis.exceptions import WatchError

    while True:
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(key(machine))
                now = lease.now_ms(store)
                taken, settled = _held(store, pipe.hgetall(key(machine)), f"{slug}/{task}", now)
                if len(taken) >= local_max:
                    return len(taken)
                pipe.multi()
                if settled:
                    pipe.hdel(key(machine), *settled)
                pipe.hset(key(machine), f"{slug}/{name}", now)
                pipe.execute()
                return len(taken)
            except WatchError:
                continue


def release(store: RedisStore, machine: str, slug: str, name: str) -> None:
    store.redis.hdel(key(machine), f"{slug}/{name}")


def _held(store: RedisStore, claims: dict, own: str, now: int) -> tuple[list[str], list[str]]:
    records = {
        f"{slug}/{agent.name}": (f"{slug}/{agent.task}", agent)
        for slug in store.slugs()
        for agent in store.agents(slug)
    }
    agents = [named for named, (task, agent) in records.items() if task != own and _local_worker(agent)]
    pending = [named for named, at in claims.items() if _pending(records.get(named), now - int(at))]
    return [*agents, *pending], [named for named in claims if named not in pending]


def _pending(record, age: int) -> bool:
    return age < PENDING_MS and (record is None or record[1].state == STARTING)


def _local_worker(agent) -> bool:
    return (
        agent.runtime_backend == LOCAL
        and not agent.hive
        and agent.lane != "master"
        and agent.state not in (STARTING, "finished")
    )
