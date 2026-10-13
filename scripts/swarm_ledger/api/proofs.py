import hashlib
import hmac
import json
import sys
from collections.abc import Callable
from types import ModuleType

from scripts.swarm import naming

from . import schemas
from .errors import APIError

CREATE = {
    "type": "object",
    "required": ["agent", "task", "content"],
    "additionalProperties": False,
    "properties": {"agent": {"type": "string"}, "task": {"type": "string"}, "content": {"type": "object"}},
}


def require_controller(handler: object, server: ModuleType) -> None:
    if not server.authority.controller(handler.headers.get("X-Controller-Credential")):
        raise APIError(403, "forbidden", "Proof ledgers need the controller credential")


def route(handler: object, server: ModuleType, parts: list, read_body: Callable[[object, ModuleType], dict]) -> dict:
    if len(parts) == 3 and parts[2] == "credential":
        schemas.validate({"type": "object", "maxProperties": 0}, read_body(handler, server))
        return credential(handler, server, parts[1])
    if len(parts) == 3 and parts[2] == "controls":
        return control(handler, server, parts[1], read_body(handler, server))
    require_controller(handler, server)
    if len(parts) == 1:
        return create(server, read_body(handler, server))
    if len(parts) == 3 and parts[2] == "remove":
        schemas.validate({"type": "object", "maxProperties": 0}, read_body(handler, server))
        return remove(handler, server, parts[1])
    raise APIError(404, "resource_missing", "No such proof resource")


def create(server: ModuleType, payload: dict) -> dict:
    import new_ledger

    schemas.validate(CREATE, payload)
    taken = {row["slug"] for row in server.repository.list_summaries() if row["slug"].startswith("proof-")}
    environ = {"AGENTIHOOKS_AGENT_NAME": payload["agent"], "AGENTIHOOKS_SWARM_TASK": payload["task"]}
    try:
        slug = naming.proof_slug(environ, taken)
    except naming.NamingError as exc:
        raise APIError(400, "schema_invalid", str(exc)) from None
    errors = new_ledger.check(payload["content"])
    if errors:
        raise APIError(400, "schema_invalid", "Content rejected: " + "; ".join(errors))
    try:
        created = server.repository.create(slug, payload["content"])
    except ValueError as exc:
        raise APIError(400, "schema_invalid", f"Content rejected: {exc}") from None
    if not created:
        raise APIError(409, "ledger_exists", "Ledger exists")
    store = server.swarm_store()
    owner = payload["agent"]
    parent = store.names.slug_of(owner)
    if parent and naming.lane_of(owner) == "eng":
        store.redis.hset(store.key(slug, "proof-creator"), mapping={"parent": parent, "owner": owner})
    return {"data": {"slug": slug, "created": True}}


def _owner(server: ModuleType, slug: str) -> dict:
    if not naming.PROOF_RE.fullmatch(slug) or not server.repository.exists(slug):
        raise APIError(403, "forbidden", "Not an owned proof fixture")
    store = server.swarm_store()
    owner = store.redis.hgetall(store.key(slug, "proof-creator"))
    if not owner:
        raise APIError(403, "forbidden", "Not an owned proof fixture")
    return owner


def _credential(server: ModuleType, slug: str, owner: dict) -> str:
    key = server.repository.token(slug)
    if not key:
        raise APIError(403, "forbidden", "Proof fixture has no credential")
    scope = json.dumps(["proof-controls", slug, owner["parent"], owner["owner"]], separators=(",", ":"))
    return hmac.new(key.encode(), scope.encode(), hashlib.sha256).hexdigest()


def credential(handler: object, server: ModuleType, slug: str) -> dict:
    owner = _owner(server, slug)
    caller = server.authority.principal(
        server.repository.token(owner["parent"]),
        owner["parent"],
        handler.headers.get("X-Ledger-Token"),
        handler.headers.get("X-Ledger-Agent"),
    )
    if caller != owner["owner"]:
        raise APIError(403, "forbidden", "Only the creating engineer receives the fixture credential")
    return {"data": {"credential": _credential(server, slug, owner)}}


def control(handler: object, server: ModuleType, slug: str, payload: dict) -> dict:
    from scripts.swarm import commands
    from scripts.swarm.lifecycle import Refused

    owner = _owner(server, slug)
    token = handler.headers.get("X-Proof-Credential")
    if not token or not hmac.compare_digest(token.encode(), _credential(server, slug, owner).encode()):
        raise APIError(403, "forbidden", "Wrong proof fixture credential")
    store = server.swarm_store()
    if not store.config(slug).scratch:
        raise APIError(403, "forbidden", "Proof controls need a scratch swarm")
    action = payload.get("control") if isinstance(payload, dict) else None
    if action == "pause" and payload == {"control": "pause"}:
        return {"data": commands.submit(store, slug, "swarm", ["pause"])}
    if action != "drain":
        raise APIError(403, "forbidden", "Proof controls permit only pause and drain")
    try:
        result, error = server.lifecycle_control(slug, payload, False)
    except Refused as exc:
        raise APIError(exc.status, "control_refused", str(exc)) from None
    if error:
        raise APIError(409, "control_refused", error)
    return {"data": result}


def remove(handler: object, server: ModuleType, slug: str) -> dict:
    if not naming.PROOF_RE.fullmatch(slug):
        raise APIError(403, "forbidden", "Not a proof ledger")
    if not handler.exists(slug):
        raise APIError(404, "ledger_missing", "No such ledger")
    server.ledger_bin.delete(slug)
    store = server.swarm_store()
    store.redis.delete(store.key(slug, "proof-creator"))
    data = {"slug": slug, "removed_at": server.ledger_bin.entries()[slug]}
    if server.swarm_status(slug) is not None:
        _, error = server.swarm_control(slug, ["stop", "--now"])
        if error:
            data["swarm_error"] = error
    sys.stderr.write(f"controller removed proof ledger {slug} at {data['removed_at']}\n")
    return {"data": data}
