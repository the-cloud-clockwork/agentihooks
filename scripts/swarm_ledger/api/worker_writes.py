from types import SimpleNamespace

from scripts.swarm_ledger import ports

from .errors import APIError
from .schemas import target


def check(slug: str, doc: dict, op: dict) -> None:
    if "worker_claim" not in op:
        return
    swarm = ports.swarm()
    SwarmError, connect = swarm.store.SwarmError, swarm.store.connect
    spec_revision, TaskAuthority = swarm.task_specs.spec_revision, swarm.authority.TaskAuthority

    pin = op["worker_claim"]
    store = connect()
    authority = TaskAuthority(store, SimpleNamespace(slug=slug), None)
    scope = SimpleNamespace(execution_id=pin["execution_id"], generation=pin["execution_generation"])
    try:
        claim = authority.current(pin["task_id"])
        authority._holder(scope, pin["task_generation"], claim)
        if claim.holder != op["by"]:
            raise SwarmError
    except SwarmError:
        refuse(store, slug, op, "stale_generation", "Stale worker claim")
    if target(op).split("/")[:2] != ["tasks", pin["task_id"]]:
        refuse(store, slug, op, "stale_generation", "Wrong task claim")
    task = next((task for task in doc["tasks"] if task["id"] == pin["task_id"]), {})
    if spec_revision(task) != pin["spec_revision"]:
        refuse(store, slug, op, "revision_conflict", "Changed task specification")


def refuse(store: object, slug: str, op: dict, code: str, message: str) -> None:
    key = store.key(slug, "hosted-worker-refusals")
    store.redis.hset(key, op["id"], code)
    store.redis.expire(key, 86400)
    raise APIError(409, code, message)
