import json
import time

from redis import RedisError

from scripts.swarm_ledger import ports

from . import resources, schemas
from .errors import APIError
from .routing import _principal
from .services import BodyReader, Services

registry, HiveError = ports.hive().registry, ports.hive().auth.HiveError
SwarmError = ports.swarm().store.SwarmError

FLAG = {"enum": ["yes", "no"]}
PATCH = {
    "type": "object",
    "minProperties": 1,
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 200},
        "ui": FLAG,
        "ephemeral": FLAG,
        "roles": {"type": "array", "items": {"enum": list(registry.ROLES)}},
        "prefer": {"type": "object", "additionalProperties": {"type": "integer"}},
        "max_agents": {"type": "integer"},
    },
}
MISSING = "resource_missing"


def pairs(payload: dict) -> list[str]:
    found = []
    for field, value in payload.items():
        if field == "roles":
            value = ",".join(value)
        elif field == "prefer":
            value = ",".join(f"{role}:{rank}" for role, rank in value.items())
        found.append(f"{field.replace('_', '-')}={value}")
    return found


def masters(store: object) -> dict[str, dict]:
    affinity = ports.swarm().affinity

    found = {}
    for slug in store.slugs():
        master = affinity.live_master(store, slug)
        if master is not None and master.hive:
            found[master.hive] = {"slug": slug, "name": master.name}
    return found


def view(store: object, now_ms: int) -> list[dict]:
    loaded_hives = ports.hive().launch.loaded_hives
    leads = masters(store)
    hives = loaded_hives(store)
    for hive in hives:
        sessions = json.loads(store.redis.get(f"{registry.key(hive['id'])}:sessions") or "{}")
        hive["live"] = registry.live(hive, now_ms)
        hive["master"] = leads.get(hive["id"])
        hive["slots"] = [
            {**slot, "agents": sessions.get(f"{slot['harness']}:{slot['account']}", 0)} for slot in hive["slots"]
        ]
    return hives


def one(store: object, hive_id: str, now_ms: int) -> dict:
    found = next((hive for hive in view(store, now_ms) if hive["id"] == hive_id), None)
    if found is None:
        raise APIError(404, MISSING, "No such hive")
    return found


def _write(handler: object, services: Services, store: object, hive_id: str, payload: dict) -> None:
    principal = _principal(handler, services)
    if principal is None:
        raise APIError(403, "forbidden", "Missing or wrong ledger credential")
    if principal:
        raise APIError(403, "forbidden", "Hive settings need the operator")
    schemas.validate(PATCH, payload)
    if registry.show(store.redis, hive_id) is None:
        raise APIError(404, MISSING, "No such hive")
    try:
        registry.update(store.redis, hive_id, pairs(payload))
    except HiveError as exc:
        raise APIError(400, "schema_invalid", str(exc)) from None


def route(handler: object, services: Services, parts: list, read_body: BodyReader) -> dict:
    if len(parts) > 2:
        raise APIError(404, MISSING, "No such resource")
    if handler.command not in (("GET", "PATCH") if len(parts) == 2 else ("GET",)):
        raise APIError(405, "method_not_allowed", "Use GET, or PATCH on one hive")
    try:
        store, now_ms = services.swarm.store(), int(time.time() * 1000)
        if handler.command == "PATCH":
            _write(handler, services, store, parts[1], read_body(handler, services))
        data = one(store, parts[1], now_ms) if len(parts) == 2 else view(store, now_ms)
    except (SwarmError, HiveError) as exc:
        raise APIError(503, "storage_error", str(exc)) from None
    except RedisError:
        raise APIError(503, "storage_error", "The swarm store is unreachable") from None
    return {"data": data, "revision": resources.revision(data)}
