import sys
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
    return {"data": {"slug": slug, "created": True}}


def remove(handler: object, server: ModuleType, slug: str) -> dict:
    if not naming.PROOF_RE.fullmatch(slug):
        raise APIError(403, "forbidden", "Not a proof ledger")
    if not handler.exists(slug):
        raise APIError(404, "ledger_missing", "No such ledger")
    server.ledger_bin.delete(slug)
    data = {"slug": slug, "removed_at": server.ledger_bin.entries()[slug]}
    if server.swarm_status(slug) is not None:
        _, error = server.swarm_control(slug, ["stop", "--now"])
        if error:
            data["swarm_error"] = error
    sys.stderr.write(f"controller removed proof ledger {slug} at {data['removed_at']}\n")
    return {"data": data}
