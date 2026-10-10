from types import ModuleType

from . import resources, schemas
from .errors import APIError


def layout(handler: object, server: ModuleType, payload: dict | None) -> dict:
    if handler.command == "GET":
        data = server.ledger_layout.read()
    else:
        if handler.headers.get("Origin") not in server.ALLOWED_ORIGINS:
            raise APIError(403, "forbidden", "Origin not allowed")
        properties = {
            row: {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": False,
                "properties": {key: {"type": "integer"} for key in fields},
            }
            for row, fields in server.ledger_layout.ROWS.items()
        }
        schemas.validate({"type": "object", "additionalProperties": False, "properties": properties}, payload)
        try:
            data = server.ledger_layout.write(payload)
        except ValueError:
            raise APIError(400, "schema_invalid", "Layout sizes are out of range") from None
    return {"data": data, "revision": resources.revision(data)}


def bin_action(handler: object, server: ModuleType, payload: dict) -> dict:
    if handler.headers.get("Origin") not in server.ALLOWED_ORIGINS:
        raise APIError(403, "forbidden", "Origin not allowed")
    schemas.validate(
        {
            "type": "object",
            "required": ["action", "slug"],
            "additionalProperties": False,
            "properties": {
                "action": {"enum": ["delete", "restore"]},
                "slug": {"type": "string", "pattern": server.core.SLUG_RE.pattern},
            },
        },
        payload,
    )
    slug = payload["slug"]
    if not handler.exists(slug):
        raise APIError(404, "ledger_missing", "No such ledger")
    error = None
    if payload["action"] == "delete":
        server.ledger_bin.delete(slug)
        if server.swarm_status(slug) is not None:
            _, error = server.swarm_control(slug, ["stop", "--now"])
    elif not server.ledger_bin.restore(slug):
        raise APIError(404, "resource_missing", "Ledger is not in the bin")
    return {"slug": slug, "action": payload["action"], **({"swarm_error": error} if error else {})}


def control(server: ModuleType, slug: str, principal: str, payload: dict) -> dict:
    if principal:
        raise APIError(403, "forbidden", "Swarm controls need the operator")
    schemas.validate(CONTROL, payload)
    action = payload["action"]
    try:
        if action == "quota_refresh":
            _, error = server.refresh_quota(slug)
        else:
            command, argv = (
                ("doctor", server.DOCTOR[action])
                if action in server.DOCTOR
                else ("swarm", server.control_argv(payload))
            )
            _, error = server.swarm_control(slug, argv, command)
    except ValueError:
        raise APIError(400, "schema_invalid", "Control does not match its domain schema") from None
    if error:
        raise APIError(502, "control_failed", "Swarm control failed")
    return {"action": action, "accepted": True}


def upload(handler: object, server: ModuleType, slug: str, path: str) -> None:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        raise APIError(400, "schema_invalid", "Upload length must be an integer") from None
    schemas.validate(
        {
            "type": "object",
            "properties": {
                "length": {"type": "integer", "minimum": 1},
                "type": {"const": "application/octet-stream"},
                "name": {"type": "string", "maxLength": 200},
            },
            "required": ["length", "type", "name"],
            "additionalProperties": False,
        },
        {
            "length": length,
            "type": handler.headers.get_content_type(),
            "name": handler.headers.get("X-Artifact-Name", ""),
        },
    )
    return handler.post_artifact(slug) if path == "uploads/artifacts" else handler.post_media(slug)


CONTROL = {
    "type": "object",
    "required": ["action"],
    "additionalProperties": False,
    "properties": {
        "action": {
            "enum": [
                "start",
                "pause",
                "stop",
                "stop_now",
                "close",
                "reopen",
                "set",
                "verdict",
                "terminate",
                "restore-decision",
                "lift",
                "quota_refresh",
                "doctor_start",
                "doctor_stop",
            ]
        },
        **{
            key: {"type": "integer"}
            for key in ("max_eng", "max_ci", "max_plan", "compact_limit", "memory_per_agent_mb")
        },
        "scaling": {"enum": ["auto", "manual"]},
        "load_high": {"type": "number"},
        "load_low": {"type": "number"},
        **{
            key: {"type": "string", "maxLength": 2000}
            for key in (
                "id",
                "name",
                "verdict",
                "note",
                "agent",
                "choice",
                "gate",
                "effort_min",
                "effort_max",
                "autonomy",
            )
        },
        "gates": {"type": "object", "additionalProperties": {"type": "string"}},
    },
}
