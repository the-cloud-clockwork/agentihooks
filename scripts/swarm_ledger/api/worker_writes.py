from types import SimpleNamespace

from scripts.swarm.store import SwarmError, connect
from scripts.swarm_v2.authority import TaskAuthority

from .errors import APIError


def check(slug: str, doc: dict, op: dict) -> None:
    if "worker_claim" not in op:
        return
    pin = op["worker_claim"]
    store = connect()
    authority = TaskAuthority(store, None, None)
    scope = SimpleNamespace(execution_id=pin["execution_id"], generation=pin["execution_generation"])
    try:
        claim = authority.current(pin["task_id"])
        authority._holder(scope, pin["task_generation"], claim)
        if claim.holder != op["by"]:
            raise SwarmError("stale_generation")
    except SwarmError:
        key = store.key(slug, "hosted-worker-refusals")
        store.redis.hset(key, op["id"], "stale_generation")
        store.redis.expire(key, 86400)
        raise APIError(409, "stale_generation", "Stale worker claim") from None
