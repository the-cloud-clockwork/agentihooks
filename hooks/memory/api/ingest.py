"""``POST /v2/sessions/ingest``: scope is checked before any write and a response is returned only after commit.

Atomicity rule: one event outside the grant, a malformed event envelope or a conflicting event rejects the whole
batch and changes nothing; an authorized event whose record is not a supported normalized record is quarantined
with its sequence and reason while the rest of the batch commits.
"""

import hashlib
import json
from dataclasses import dataclass

from hooks.memory.catalog import BatchCommit, Catalog, Conflict, StoredEvent

SCHEMA_VERSION = "2.0"
RECORD_KINDS = frozenset(
    {"user_text", "assistant_text", "system_text", "tool_call", "tool_result", "token_usage", "turn_complete", "meta"}
)
TEXT_FIELDS = ("batch_id", "access_domain", "session_uid", "source_generation")


@dataclass(frozen=True)
class Grant:
    principal: str
    corpus: str
    projects: frozenset[str]


class Rejected(Exception):
    def __init__(self, error_class: str, detail: str):
        super().__init__(detail)
        self.error_class = error_class
        self.detail = detail


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def event_identity(session_uid: str, source_generation: str, event: dict) -> str:
    return digest([session_uid, source_generation, event["sequence"], event["byte_start"], event["byte_end"]])


def _is_int(value: object, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _check_envelope(request: dict) -> None:
    if not isinstance(request, dict):
        raise Rejected("invalid_request", "request must be an object")
    version = request.get("schema_version")
    if not isinstance(version, str) or version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise Rejected("invalid_request", "unsupported schema_version")
    for name in TEXT_FIELDS:
        if not isinstance(request.get(name), str) or not request[name]:
            raise Rejected("invalid_request", f"{name} must be a non-empty string")
    events = request.get("events")
    if not isinstance(events, list) or not events:
        raise Rejected("invalid_request", "events must be a non-empty list")


def _check_event(index: int, event: object) -> None:
    if not isinstance(event, dict):
        raise Rejected("invalid_request", f"event {index} must be an object")
    if not _is_int(event.get("sequence"), 1):
        raise Rejected("invalid_request", f"event {index} sequence must be a positive integer")
    start, end = event.get("byte_start"), event.get("byte_end")
    if not (_is_int(start, 0) and _is_int(end, 0) and start <= end):
        raise Rejected("invalid_request", f"event {index} byte range is invalid")
    project = event.get("project_id")
    if project is not None and not isinstance(project, str):
        raise Rejected("invalid_request", f"event {index} project_id must be a string or null")


def _check_events(events: list) -> None:
    for index, event in enumerate(events):
        _check_event(index, event)
    sequences = [event["sequence"] for event in events]
    if len(set(sequences)) != len(sequences):
        raise Rejected("invalid_request", "event sequences repeat inside the batch")


def _authorize(grant: Grant, request: dict) -> None:
    if request["access_domain"] != grant.corpus:
        raise Rejected("forbidden_scope", "the grant does not cover the requested corpus")
    outside = [
        index
        for index, event in enumerate(request["events"])
        if event["project_id"] is not None and event["project_id"] not in grant.projects
    ]
    if outside:
        raise Rejected("forbidden_scope", f"events {outside} are outside the grant; the batch was not committed")


def _record_error(record: object) -> str:
    if not isinstance(record, dict):
        return "record must be an object"
    kind = record.get("kind")
    if not isinstance(kind, str) or kind not in RECORD_KINDS:
        return "record kind is not a supported normalized kind"
    return ""


def _prepare(request: dict) -> BatchCommit:
    events, rejected = [], []
    for event in request["events"]:
        identity = event_identity(request["session_uid"], request["source_generation"], event)
        error = _record_error(event.get("record"))
        if error:
            rejected.append({"sequence": event["sequence"], "event_id": identity, "reason": error})
            continue
        record = event["record"]
        events.append(
            StoredEvent(
                identity,
                event["sequence"],
                event["byte_start"],
                event["byte_end"],
                event["project_id"],
                record,
                digest(record),
            )
        )
    return BatchCommit(
        request["access_domain"],
        request["batch_id"],
        digest(request),
        request["session_uid"],
        request["source_generation"],
        events,
        rejected,
    )


def _refusal(request: object, error: Rejected) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_id": f"ingest:{digest(request)[:16]}",
        "state": "rejected",
        "error": {"class": error.error_class, "retry": "never", "detail": error.detail},
    }


def ingest(catalog: Catalog, grant: Grant, request: dict) -> dict:
    try:
        _check_envelope(request)
        _check_events(request["events"])
        _authorize(grant, request)
        result = catalog.commit(_prepare(request))
    except Conflict as conflict:
        return _refusal(request, Rejected("revision_conflict", str(conflict)))
    except Rejected as error:
        return _refusal(request, error)
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": request["batch_id"],
        "session_uid": request["session_uid"],
        "source_generation": request["source_generation"],
        **result,
        "indexed": False,
    }
