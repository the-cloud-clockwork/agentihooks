from collections.abc import Mapping
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts.swarm_ledger import ports

from . import resources, schemas
from .errors import APIError
from .services import Services

records, operator_auth = ports.swarm().architecture, ports.swarm().operator_auth
ROOT = Path(records.__file__).resolve().parents[2]
RECORD_ENV = "AGENTIHOOKS_ARCHITECTURE_RECORD"
INVENTORY_ENV = "AGENTIHOOKS_ARCHITECTURE_INVENTORY"
ACTION = {
    "oneOf": [
        {
            "type": "object",
            "required": ["action", "proposal", "reason"],
            "additionalProperties": False,
            "properties": {
                "action": {"const": "approve"},
                "proposal": {"type": "string", "minLength": 1, "maxLength": 200},
                "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            },
        },
        {
            "type": "object",
            "required": ["action", "to"],
            "additionalProperties": False,
            "properties": {"action": {"const": "rollback"}, "to": {"type": "integer", "minimum": 1}},
        },
    ]
}


def paths(environ: Mapping[str, str]) -> tuple[Path, Path | None]:
    inventory = environ.get(INVENTORY_ENV)
    return Path(environ.get(RECORD_ENV) or ROOT / records.RECORD), Path(inventory) if inventory else None


def view(environ: Mapping[str, str], key: Ed25519PublicKey | None = None) -> dict:
    record_path, inventory_path = paths(environ)
    record = records.load_record(record_path)
    key = key or records.verify_key(environ)
    proposals = records.load_inventory(inventory_path)["proposals"] if inventory_path else []
    operations = {o["revision"]: o["id"] for o in record["operations"]}
    data = {
        "revision": record["revision"],
        "components": len(record["components"]),
        "proposals": [
            {"id": p["id"], "name": p["name"], "kind": p["kind"], "approved": records.approved(record, p, key)}
            for p in proposals
            if records.dispatches(p)
        ],
        "revisions": [
            {"revision": n, "operation": operations.get(n, "")} for n in range(record["revision"] - 1, 0, -1)
        ],
    }
    return {"data": data, "revision": resources.revision(data)}


def operator_transport(handler: object, services: Services, slug: str, principal: str) -> dict:
    credential = handler.headers.get("X-Ledger-Token")
    authenticate = operator_auth.authenticator(services.repository.token)
    if principal or authenticate(slug, credential) is None:
        raise APIError(403, "forbidden", "Architecture changes need the operator")
    return {"slug": slug, "credential": credential, "authenticate": authenticate}


def act(
    handler: object, services: Services, slug: str, principal: str, payload: dict, environ: Mapping[str, str]
) -> dict:
    transport = operator_transport(handler, services, slug, principal)
    schemas.validate(ACTION, payload)
    record_path, inventory_path = paths(environ)
    try:
        if payload["action"] == "approve":
            if inventory_path is None:
                raise records.ArchitectureError("no design inventory is configured")
            _, key = records.operator_approval(
                record_path, inventory_path, payload["proposal"], payload["reason"], transport, environ
            )
        else:
            current = records.load_record(record_path)["revision"]
            records.rollback(record_path, payload["to"], f"page-rollback-{current}-to-{payload['to']}", **transport)
            key = records.verify_key(environ)
    except records.ArchitectureError as exc:
        raise APIError(409, "architecture_refused", str(exc)) from None
    records.write_markdown(record_path, record_path.with_name(Path(records.MARKDOWN).name), key)
    return view(environ, key)
