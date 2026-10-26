from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from scripts.swarm_ledger import models

from .errors import APIError


def checkbox_path() -> str:
    groups = {}
    for name, model in models.MODELS.items():
        if boxes := models.checkboxes(model):
            groups.setdefault(boxes, []).append(name)
    paths = (f"({'|'.join(names)})/[^/]+/({'|'.join(boxes)})" for boxes, names in groups.items())
    return f"^({'|'.join(paths)})$"


MUTATION = {
    "type": "object",
    "required": ["operation_id", "ops", "guards"],
    "additionalProperties": False,
    "properties": {
        "operation_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "ops": {"type": "array", "maxItems": 100, "items": {"type": "object"}},
        "id": {"type": "string", "minLength": 1, "maxLength": 200},
        "changes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 100,
            "items": {
                "type": "object",
                "required": ["path", "value"],
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "pattern": checkbox_path()},
                    "base": {"type": "boolean"},
                    "value": {"type": "boolean"},
                },
            },
        },
        "guards": {"type": "object", "additionalProperties": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
    },
}


from types import ModuleType


def validate(schema: dict, body: object) -> None:
    if not Draft202012Validator(schema).is_valid(body):
        raise APIError(400, "schema_invalid", "Request does not match the resource schema")


PAGINATION = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "cursor": {"type": "string", "pattern": "^[a-f0-9]{64}:[0-9]{1,12}$"},
        "state": {"enum": ["done"]},
        "offset": {"type": "integer", "minimum": 0, "maximum": 1000000},
        "q": {"type": "string", "maxLength": 200},
    },
}

FIELDS = {
    "add": "thread text by long attachments to reply_to outcome notice",
    "edit": "thread text by",
    "delete": "thread by",
    "clear": "thread",
    "sync": "",
    "stats_sync": "",
    "join": "by role",
    "leave": "by",
    "ack": "by rev",
    "claim": "by item status",
    "set": "by path value status",
    "add_item": "by list text needs_operator hive task not_duplicate",
    "retext": "by item text",
    "gate_bypass": "by unhandled",
    "gate_lift": "by gate",
    "priority": "by item text",
    "priority_clear": "by target reason",
    "notification_clear": "target",
    "notice": "by text",
    "task_add": "by task title lane phase description depends_on territory overlays repositories kind contract proof workspace artifact profile plan_url plan_slice rank gain difficulty difficulty_source difficulty_confidence not_duplicate slice follow_up workstation needs",
    "task_update": "by item fields if_state if_match if_plan_lines_missing quote",
    "task_rank": "by item rank if_owned",
    "task_group": "by item members",
    "task_ungroup": "by item",
    "task_split": "by item halves",
    "title_set": "text",
    "overview_set": "text",
    "agent_rename": "by old new",
    "summary_set": "by note",
    "close": "by",
    "reopen": "by",
    "size_set": "by size",
    "source_add": "by source",
    "phase_add": "by phase title description depends_on planning release plan_url plan_ref plan not_duplicate",
    "plan_add": "by plan title artifact url source",
    "slice_add": "by phase anchor",
    "phase_update": "by item fields",
    "phase_review": "by item state note override rounds escalated",
    "phase_append": "by phases",
    "relay": "by item text quote",
    "answer": "by item text",
    "verdict": "item verdict",
    "artifact_add": "by task title file request plan",
    "artifact_delete": "target",
    "artifact_restore": "target",
    "artifact_purge": "by",
    "alert_claim": "by target",
    "alert_close": "by target item outcome",
    "alert_raise": "by source text item",
    "time_left": "by slots ci_minutes fix_phase watched",
    "freeze_set": "by verb target reason quote",
    "freeze_clear": "by target reason quote",
    "events_ack": "by rev",
    "blocker_attempt": "by item text command output",
    "blocker_critic": "by item run digest resolution",
    "blocker_escalate": "by item reason evidence",
}
TYPES = {
    "long": {"type": "boolean"},
    "notice": {"const": True},
    "to": {"const": "operator"},
    "needs_operator": {"type": "boolean"},
    "artifact": {"type": "boolean"},
    "follow_up": {"type": "boolean"},
    "workstation": {"type": "boolean"},
    "release": {"type": "boolean"},
    "if_plan_lines_missing": {"type": "boolean"},
    "rounds": {"type": "integer", "minimum": 0},
    "escalated": {"type": "boolean"},
    "rev": {"type": "integer", "minimum": 0},
    "unhandled": {"type": "integer", "minimum": 0},
    "gain": {"type": "number", "minimum": 0},
    "difficulty_confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "slots": {"type": ["integer", "null"], "minimum": 0},
    "ci_minutes": {"type": ["number", "null"], "minimum": 0},
    "value": {"type": ["boolean", "integer"]},
    "fields": {"type": "object"},
    "if_match": {"type": "object"},
    "contract": {"type": "object"},
    "proof": {"type": "object"},
    "file": {"type": "object"},
    "outcome": {"type": "string", "minLength": 1, "maxLength": 2000},
    "quote": {"type": "string", "minLength": 1},
    "override": {"type": "object"},
    "phases": {"type": "array", "maxItems": 100, "items": {"type": "object"}},
    "attachments": {"type": "array", "maxItems": 100, "items": {"type": "object"}},
}
KIND_TYPES = {
    "artifact_add": {"plan": {"type": "boolean"}},
    "plan_add": {"artifact": {"type": "string", "maxLength": 100000}, "source": {"type": "object"}},
    "task_rank": {"if_owned": {"type": "boolean"}},
}
for _field in (
    "depends_on",
    "territory",
    "overlays",
    "repositories",
    "needs",
    "if_state",
    "members",
    "watched",
    "halves",
):
    TYPES[_field] = {"type": "array", "maxItems": 100, "items": {"type": "string", "maxLength": 2000}}


def operation_schema(kind: str) -> dict:
    types = {**TYPES, **KIND_TYPES.get(kind, {})}
    properties = {key: types.get(key, {"type": "string", "maxLength": 100000}) for key in FIELDS[kind].split()}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["op", "id"],
        "properties": {
            **properties,
            "op": {"const": kind},
            "id": {"type": "string", "minLength": 1, "maxLength": 200},
            "controller_epoch": {"type": "integer", "minimum": 1},
            "worker_claim": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_id", "execution_id", "execution_generation", "task_generation", "spec_revision"],
                "properties": {
                    "task_id": {"type": "string", "minLength": 1},
                    "execution_id": {"type": "string", "minLength": 1},
                    "execution_generation": {"type": "integer", "minimum": 1},
                    "task_generation": {"type": "integer", "minimum": 1},
                    "spec_revision": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                },
            },
        },
    }


def mismatched_field(schema: dict, operation: dict) -> str | None:
    error = best_match(Draft202012Validator(schema).iter_errors(operation))
    if error is None:
        return None
    if error.absolute_path:
        return str(error.absolute_path[0])
    if error.validator == "required":
        return next(key for key in schema["required"] if key not in operation)
    return sorted(set(operation) - set(schema["properties"]))[0]


def check_operations(payload: dict, core: ModuleType, task_ids: tuple) -> list:
    validate(MUTATION, payload)
    operations = list(payload["ops"])
    if payload.get("changes"):
        if "id" not in payload or any(op.get("id") == payload["id"] for op in operations):
            raise APIError(400, "schema_invalid", "Checkbox changes need a distinct operation identifier")
    elif not operations:
        raise APIError(400, "schema_invalid", "At least one operation is required")
    for operation in operations:
        kind = operation.get("op")
        if kind not in FIELDS:
            raise APIError(400, "schema_invalid", "Unknown operation")
        field = mismatched_field(operation_schema(kind), operation)
        if field is not None:
            raise APIError(400, "schema_invalid", f"Operation {kind} does not match its schema at field {field}")
    try:
        core.check_body(
            {"ops": [{k: v for k, v in op.items() if k != "controller_epoch"} for op in operations]},
            task_ids,
        )
    except ValueError as exc:
        raise APIError(400, "schema_invalid", f"Operation does not match its domain schema: {exc}") from None
    if len({op["id"] for op in operations}) != len(operations):
        raise APIError(400, "schema_invalid", "Operation identifiers must be distinct")
    if payload.get("changes"):
        operations.insert(0, {"op": "sync", "id": payload["id"]})
    return operations


UNGUARDED = ("ack", "task_add")


def target(op: dict) -> str:
    kind = op["op"]
    if kind in ("add", "edit", "delete", "clear"):
        return op["thread"]
    if kind in ("join", "leave", "ack", "agent_rename"):
        return "members"
    if kind == "set":
        return op["path"].rsplit("/", 1)[0] if "/" in op["path"] else "metadata"
    if kind == "add_item":
        return op["list"]
    if kind.startswith("task_"):
        return op.get("item", "tasks")
    if kind.startswith("phase_"):
        return op.get("item", "phases")
    if kind.startswith("priority"):
        return "priorities"
    if kind in ("notification_clear", "notice"):
        return "notifications"
    if kind.startswith("artifact_"):
        return "artifacts"
    if kind in ("claim", "retext", "relay", "answer", "verdict") or kind.startswith("blocker_"):
        return op["item"]
    return {
        "alert_claim": "alerts",
        "alert_close": "alerts",
        "alert_raise": "alerts",
        "source_add": "sources",
        "plan_add": "plans",
        "slice_add": "slices",
        "freeze_set": "freezes",
        "freeze_clear": "freezes",
    }.get(kind, "metadata")
