import hashlib
import hmac
import json
import sys

from scripts import agent_choice
from scripts.swarm_ledger import ports

from . import schemas
from .errors import APIError
from .services import BodyReader, Services

naming, templates = ports.swarm().naming, ports.swarm().templates
CODEX_TRANSPORTS = ports.swarm().store.CODEX_TRANSPORTS
CREATE = {
    "type": "object",
    "required": ["agent", "task", "content"],
    "additionalProperties": False,
    "properties": {"agent": {"type": "string"}, "task": {"type": "string"}, "content": {"type": "object"}},
}
CAPS = ("max-eng-agents", "max-ci-agents", "max-plan-agents")
SETTABLE_FIELDS = frozenset(
    {"codex-inbox-transport", *CAPS}
    | {f"{lane}-{field}" for lane in templates.LANES for field in ("agent", "model", "effort", "profile")}
)
START = {
    "type": "object",
    "required": ["control"],
    "additionalProperties": False,
    "properties": {"control": {"const": "start"}},
}
SET = {
    "type": "object",
    "required": ["control", "fields"],
    "additionalProperties": False,
    "properties": {
        "control": {"const": "set"},
        "fields": {
            "type": "object",
            "minProperties": 1,
            "properties": {
                "codex-inbox-transport": {"enum": list(CODEX_TRANSPORTS)},
                **{cap: {"type": "string", "pattern": r"^[0-9]{1,3}\Z"} for cap in CAPS},
                **{f"{lane}-agent": {"enum": [templates.AUTO, *agent_choice.AGENTS]} for lane in templates.LANES},
            },
            "additionalProperties": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z"},
        },
    },
}


def require_controller(handler: object, services: Services) -> None:
    if not services.ledger.authority.controller(handler.headers.get("X-Controller-Credential")):
        raise APIError(403, "forbidden", "Proof ledgers need the controller credential")


def route(handler: object, services: Services, parts: list, read_body: BodyReader) -> dict:
    if len(parts) == 3 and parts[2] == "credential":
        schemas.validate({"type": "object", "maxProperties": 0}, read_body(handler, services))
        return credential(handler, services, parts[1])
    if len(parts) == 3 and parts[2] == "controls":
        return control(handler, services, parts[1], read_body(handler, services))
    require_controller(handler, services)
    if len(parts) == 1:
        return create(services, read_body(handler, services))
    if len(parts) == 3 and parts[2] == "migrate":
        return migrate(services, parts[1], read_body(handler, services))
    if len(parts) == 3 and parts[2] == "controller-controls":
        return controller_control(services, parts[1], read_body(handler, services))
    if len(parts) == 3 and parts[2] == "remove":
        schemas.validate({"type": "object", "maxProperties": 0}, read_body(handler, services))
        return remove(handler, services, parts[1])
    raise APIError(404, "resource_missing", "No such proof resource")


def create(services: Services, payload: dict) -> dict:
    import new_ledger

    schemas.validate(CREATE, payload)
    taken = {row["slug"] for row in services.repository.list_summaries() if row["slug"].startswith("proof-")}
    environ = {"AGENTIHOOKS_AGENT_NAME": payload["agent"], "AGENTIHOOKS_SWARM_TASK": payload["task"]}
    try:
        slug = naming.proof_slug(environ, taken)
    except naming.NamingError as exc:
        raise APIError(400, "schema_invalid", str(exc)) from None
    errors = new_ledger.check(payload["content"])
    if errors:
        raise APIError(400, "schema_invalid", "Content rejected: " + "; ".join(errors))
    try:
        created = services.repository.create(slug, payload["content"])
    except ValueError as exc:
        raise APIError(400, "schema_invalid", f"Content rejected: {exc}") from None
    if not created:
        raise APIError(409, "ledger_exists", "Ledger exists")
    store = services.swarm.store()
    owner = payload["agent"]
    parent = store.names.slug_of(owner)
    if parent and naming.lane_of(owner) == "eng":
        store.redis.hset(store.key(slug, "proof-creator"), mapping={"parent": parent, "owner": owner})
    return {"data": {"slug": slug, "created": True}}


def _owner(services: Services, slug: str) -> dict:
    if not naming.PROOF_RE.fullmatch(slug) or not services.repository.exists(slug):
        raise APIError(403, "forbidden", "Not an owned proof fixture")
    store = services.swarm.store()
    owner = store.redis.hgetall(store.key(slug, "proof-creator"))
    if not owner:
        raise APIError(403, "forbidden", "Not an owned proof fixture")
    return owner


def _migration_owner(services: Services, slug: str, payload: dict) -> dict:
    schemas.validate(
        {
            "type": "object",
            "required": ["parent", "agent", "task"],
            "additionalProperties": False,
            "properties": {
                "parent": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$"},
                "agent": {"type": "string"},
                "task": {"type": "string", "pattern": "^[a-z0-9]+$"},
            },
        },
        payload,
    )
    store = services.swarm.store()
    if (
        not naming.PROOF_RE.fullmatch(slug)
        or not services.repository.exists(slug)
        or slug in services.ledger.bin_entries()
        or slug not in store.slugs()
    ):
        raise APIError(403, "forbidden", "Migration needs an existing scratch proof")
    if not store.config(slug).scratch:
        raise APIError(403, "forbidden", "Migration needs an existing scratch proof")
    agent = naming.parse(payload["agent"])
    if (
        not agent
        or naming.lane_of(payload["agent"]) != "eng"
        or not slug.startswith(f"proof-{agent.code}-{payload['task']}-")
    ):
        raise APIError(403, "forbidden", "Migration must match the proof engineer and task")
    return {"parent": payload["parent"], "owner": payload["agent"]}


def migrate(services: Services, slug: str, payload: dict) -> dict:
    from redis.exceptions import WatchError

    owner = _migration_owner(services, slug, payload)
    store = services.swarm.store()
    key = store.key(slug, "proof-creator")
    try:
        with store.redis.pipeline() as pipe:
            pipe.watch(key)
            saved = pipe.hgetall(key)
            if saved and saved != owner:
                raise APIError(409, "ownership_conflict", "Proof creator already differs from the requested migration")
            if not saved:
                pipe.multi()
                pipe.hset(key, mapping=owner)
                pipe.execute()
    except WatchError:
        raise APIError(409, "ownership_conflict", "Proof creator changed during migration") from None
    return {"data": owner}


def controller_control(services: Services, slug: str, payload: dict) -> dict:
    from types import SimpleNamespace

    schemas.validate(
        {
            "type": "object",
            "required": ["owner", "request"],
            "additionalProperties": False,
            "properties": {"owner": {}, "request": {"type": "object"}},
        },
        payload,
    )
    owner = _migration_owner(services, slug, payload["owner"])
    if owner != _owner(services, slug):
        raise APIError(409, "ownership_conflict", "Proof creator differs from the configured requester")
    if payload["request"] == {"control": "pause"} and services.swarm.store().config(slug).state == "paused":
        return {"data": {"state": "paused"}}
    handler = SimpleNamespace(headers={"X-Proof-Credential": _credential(services, slug, owner)})
    return control(handler, services, slug, payload["request"])


def _credential(services: Services, slug: str, owner: dict) -> str:
    key = services.repository.token(slug)
    if not key:
        raise APIError(403, "forbidden", "Proof fixture has no credential")
    scope = json.dumps(["proof-controls", slug, owner["parent"], owner["owner"]], separators=(",", ":"))
    return hmac.new(key.encode(), scope.encode(), hashlib.sha256).hexdigest()


def credential(handler: object, services: Services, slug: str) -> dict:
    owner = _owner(services, slug)
    caller = services.ledger.authority.caller(
        handler.headers, services.repository.token(owner["parent"]), owner["parent"]
    )
    if caller != owner["owner"]:
        raise APIError(403, "forbidden", "Only the creating engineer receives the fixture credential")
    return {"data": {"credential": _credential(services, slug, owner)}}


def _fixture_argv(payload: dict) -> list:
    if payload["control"] == "start":
        schemas.validate(START, payload)
        return ["start"]
    schemas.validate(SET, payload)
    for key in sorted(payload["fields"]):
        if key not in SETTABLE_FIELDS:
            raise APIError(403, "forbidden", f"Proof controls cannot set {key}")
    return ["set", *(f"{key}={payload['fields'][key]}" for key in sorted(payload["fields"]))]


def control(handler: object, services: Services, slug: str, payload: dict) -> dict:
    commands = ports.swarm().commands
    Refused = ports.swarm().lifecycle.Refused

    owner = _owner(services, slug)
    token = handler.headers.get("X-Proof-Credential")
    if not token or not hmac.compare_digest(token.encode(), _credential(services, slug, owner).encode()):
        raise APIError(403, "forbidden", "Wrong proof fixture credential")
    store = services.swarm.store()
    if not store.config(slug).scratch:
        raise APIError(403, "forbidden", "Proof controls need a scratch swarm")
    action = payload.get("control") if isinstance(payload, dict) else None
    if action == "pause" and payload == {"control": "pause"}:
        return {"data": commands.submit(store, slug, "swarm", ["pause"])}
    if action in ("set", "start"):
        return {"data": commands.submit(store, slug, "swarm", _fixture_argv(payload))}
    if action != "drain":
        raise APIError(403, "forbidden", "Proof controls permit only pause, drain, set and start")
    try:
        result, error = services.swarm.lifecycle(slug, payload, False)
    except Refused as exc:
        raise APIError(exc.status, "control_refused", str(exc)) from None
    if error:
        raise APIError(409, "control_refused", error)
    return {"data": result}


def remove(handler: object, services: Services, slug: str) -> dict:
    if not naming.PROOF_RE.fullmatch(slug):
        raise APIError(403, "forbidden", "Not a proof ledger")
    if not handler.exists(slug):
        raise APIError(404, "ledger_missing", "No such ledger")
    services.ledger.bin_ledger(slug)
    store = services.swarm.store()
    store.redis.delete(store.key(slug, "proof-creator"))
    data = {"slug": slug, "removed_at": services.ledger.bin_entries()[slug]}
    if services.swarm.status(slug) is not None:
        _, error = services.swarm.control(slug, ["stop", "--now"])
        if error:
            data["swarm_error"] = error
    sys.stderr.write(f"controller removed proof ledger {slug} at {data['removed_at']}\n")
    return {"data": data}
