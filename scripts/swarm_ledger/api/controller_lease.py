from dataclasses import asdict
from typing import Any

from scripts.swarm_ledger import ports

from . import schemas
from .errors import APIError

REQUIRED = {
    "acquire": ["owner"],
    "renew": ["owner", "epoch"],
    "release": ["owner", "epoch"],
    "transfer": ["owner", "source_epoch", "expected_epoch"],
}
EPOCH = {"type": "integer", "minimum": 0}
SCHEMA = {
    "type": "object",
    "required": ["action"],
    "additionalProperties": False,
    "properties": {
        "action": {"enum": list(REQUIRED)},
        "owner": {"type": "string", "minLength": 1},
        "epoch": EPOCH,
        "source_epoch": EPOCH,
        "expected_epoch": EPOCH,
    },
}


def connect() -> Any:
    return ports.swarm().store.connect()


def _result(value: Any) -> dict[str, Any]:
    return {"data": asdict(value) if isinstance(value, ports.swarm().lease.Lease) else value}


def read(slug: str) -> dict[str, Any]:
    return _result(ports.swarm().lease.current(connect(), slug))


def act(slug: str, principal: str, payload: dict[str, Any]) -> dict[str, Any]:
    if principal:
        raise APIError(403, "controller_access_required", "Controller access required")
    schemas.validate(SCHEMA, payload)  # pyright: ignore[reportUnknownMemberType]
    action = payload["action"]
    schemas.validate({**SCHEMA, "required": ["action", *REQUIRED[action]]}, payload)  # pyright: ignore[reportUnknownMemberType]
    lease, SwarmError = ports.swarm().lease, ports.swarm().store.SwarmError
    store = connect()
    try:
        if action == "transfer":
            return _result(
                lease.transfer(store, slug, payload["owner"], payload["source_epoch"], payload["expected_epoch"])
            )
        if action == "acquire":
            return _result(lease.acquire(store, slug, payload["owner"]))
        held = lease.current(store, slug)
        if held is None or (held.owner, held.epoch) != (payload["owner"], payload["epoch"]):
            raise SwarmError("the controller lease is stale")
        return _result(lease.renew(store, slug, held) if action == "renew" else lease.release(store, slug, held))
    except SwarmError as exc:
        raise APIError(409, "stale_controller", str(exc)) from exc
