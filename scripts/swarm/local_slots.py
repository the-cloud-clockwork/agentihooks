from scripts.swarm import lease
from scripts.swarm.store import PREFIX, RedisStore
from scripts.swarm_v2.runtime.base import LOCAL

PENDING_MS = 300000
STARTING = "starting"


def key(machine: str) -> str:
    return f"{PREFIX}:local-slots:{machine}"


def held(store: RedisStore, machine: str, slug: str, task: str) -> int:
    return len(_held(store, store.redis.hgetall(key(machine)), f"{slug}/{task}", lease.now_ms(store))[0])


def running(store: RedisStore, slug: str, task: str) -> int:
    return len(_workers(_records(store), f"{slug}/{task}"))


def maximum(store: RedisStore, slug: str, ceiling: int) -> int:
    from scripts.hive import registry
    from scripts.swarm.commands import hive_id

    hive = registry.show(store.redis, hive_id())
    bound = min(ceiling, store.config(slug).local_max_agents)
    return min(bound, hive["max_agents"]) if hive is not None else bound


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
    records = _records(store)
    agents = _workers(records, own)
    pending = [
        named
        for named, at in claims.items()
        if named.split("/", 1)[1] not in agents and _pending(records.get(named), own, now - int(at))
    ]
    return [*agents, *pending], [named for named in claims if named not in pending]


def _records(store: RedisStore) -> dict:
    return {
        f"{slug}/{agent.name}": (f"{slug}/{agent.task}", agent)
        for slug in store.slugs()
        for agent in store.agents(slug)
    }


def live() -> set[str]:
    from scripts.terminate_agent import sessions

    return {row.name for row in sessions() if row.name and not row.name.startswith("master@")}


def _workers(records: dict, own: str) -> set[str]:
    excluded = {agent.name for task, agent in records.values() if task == own}
    stored = {agent.name for task, agent in records.values() if _local_worker(agent)}
    return (stored | live()) - excluded


def _pending(record, own: str, age: int) -> bool:
    return age < PENDING_MS and (record is None or (record[0] != own and record[1].state == STARTING))


def _local_worker(agent) -> bool:
    from scripts.swarm.commands import hive_id

    return (
        agent.runtime_backend == LOCAL
        and agent.hive in ("", hive_id())
        and agent.lane != "master"
        and agent.state not in (STARTING, "finished", "ended")
    )
