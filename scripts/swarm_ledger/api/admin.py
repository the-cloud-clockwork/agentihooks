from scripts.swarm_ledger import ports

from . import resources, schemas
from .errors import APIError
from .services import Services


def layout(handler: object, services: Services, payload: dict | None) -> dict:
    if handler.command == "GET":
        data = services.ledger.layout()
    else:
        if handler.headers.get("Origin") not in services.policy.allowed_origins:
            raise APIError(403, "forbidden", "Origin not allowed")
        properties = {
            row: {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": False,
                "properties": {key: {"type": "number"} for key in fields},
            }
            for row, fields in services.ledger.layout_rows.items()
        }
        properties.update(dict.fromkeys(services.ledger.layout_lists, {}))
        schemas.validate({"type": "object", "additionalProperties": False, "properties": properties}, payload)
        try:
            data = services.ledger.write_layout(payload)
        except ValueError:
            raise APIError(400, "schema_invalid", "Layout sizes are out of range") from None
    return {"data": data, "revision": resources.revision(data)}


def bin_action(handler: object, services: Services, payload: dict) -> dict:
    if handler.headers.get("Origin") not in services.policy.allowed_origins:
        raise APIError(403, "forbidden", "Origin not allowed")
    schemas.validate(
        {
            "type": "object",
            "required": ["action", "slug"],
            "additionalProperties": False,
            "properties": {
                "action": {"enum": ["delete", "restore"]},
                "slug": {"type": "string", "pattern": services.ledger.core.SLUG_RE.pattern},
            },
        },
        payload,
    )
    slug = payload["slug"]
    if not handler.exists(slug):
        raise APIError(404, "ledger_missing", "No such ledger")
    error = None
    if payload["action"] == "delete":
        services.ledger.bin_ledger(slug)
        if services.swarm.status(slug) is not None:
            _, error = services.swarm.control(slug, ["stop", "--now"])
    elif not services.repository.restore(slug):
        raise APIError(404, "resource_missing", "Ledger is not in the bin")
    return {"slug": slug, "action": payload["action"], **({"swarm_error": error} if error else {})}


def lifecycle(services: Services, principal: str, slug: str, payload: dict) -> dict:
    from scripts.swarm_ledger.repository import bin_storage

    schemas.validate(
        {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["action", "closed_at"],
                    "additionalProperties": False,
                    "properties": {
                        "action": {"const": "bin_closed"},
                        "closed_at": {"type": "integer", "minimum": 1},
                        "controller_epoch": {"type": "integer", "minimum": 1},
                    },
                },
                {
                    "type": "object",
                    "required": ["action"],
                    "additionalProperties": False,
                    "properties": {
                        "action": {"enum": ["restore", "binned"]},
                        "controller_epoch": {"type": "integer", "minimum": 1},
                    },
                },
            ],
        },
        payload,
    )
    if principal and payload["action"] == "bin_closed":
        raise APIError(403, "forbidden", "Conditional binning needs the operator or controller credential")
    if "controller_epoch" in payload:
        SwarmError = ports.swarm().store.SwarmError

        try:
            services.ledger.authority.fence(slug, payload["controller_epoch"])
        except SwarmError as exc:
            raise APIError(409, "stale_controller", str(exc)) from None
    if payload["action"] == "bin_closed":
        result = bin_storage.bin_closed(slug, payload["closed_at"])
    elif payload["action"] == "restore":
        result = bin_storage.restore(slug)
    else:
        result = slug in bin_storage.entries()
    return {"data": result}


def control(services: Services, slug: str, principal: str, payload: dict) -> dict:
    if principal:
        raise APIError(403, "forbidden", "Swarm controls need the operator")
    schemas.validate(CONTROL, payload)
    action = payload["action"]
    try:
        if action == "quota_refresh":
            _, error = services.swarm.refresh_quota(slug)
        else:
            command, argv = (
                ("doctor", services.swarm.doctor[action])
                if action in services.swarm.doctor
                else ("swarm", services.swarm.control_argv(payload))
            )
            _, error = services.swarm.control(slug, argv, command)
    except ValueError:
        raise APIError(400, "schema_invalid", "Control does not match its domain schema") from None
    if error:
        raise APIError(502, "control_failed", "Swarm control failed")
    return {"action": action, "accepted": True}


def upload(handler: object, services: Services, slug: str, path: str) -> None:
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
                "master_move",
            ]
        },
        **{
            key: {"type": "integer"}
            for key in ("max_eng", "max_ci", "max_plan", "masters_step", "compact_limit", "memory_per_agent_mb")
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
                "hive",
            )
        },
        "gates": {"type": "object", "additionalProperties": {"type": "string"}},
    },
}
