"""``POST /v2/sessions/search``: the envelope and scope are checked before any read; a scope refusal names nothing.

A refused corpus or project returns the same detail and no result titles, counts or diagnostics, whatever the
caller asked for; a vector outage answers lexical only with its reason instead of an empty result.
"""

import sqlite3

from hooks.memory import search as core
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, Rejected, digest
from hooks.memory.embeddings import Embedder

REQUEST_FIELDS = frozenset(
    {"schema_version", "access_domain", "query", "projects", "limit", "context", "group_sessions"}
)
MAX_QUERY_CHARS = 2000
SEARCH_FAILED = "the catalog failed before answering; the same search can be retried"


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(request: dict, name: str) -> str:
    value = request.get(name)
    if not isinstance(value, str) or not value.strip():
        raise Rejected("invalid_request", f"{name} must be a non-empty string")
    return value


def _check(request: object) -> core.Query:
    if not isinstance(request, dict):
        raise Rejected("invalid_request", "request must be an object")
    version = request.get("schema_version")
    if not isinstance(version, str) or version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        raise Rejected("invalid_request", "unsupported schema_version")
    if set(request) - REQUEST_FIELDS:
        raise Rejected("invalid_request", "request carries unknown fields")
    domain, text = _text(request, "access_domain"), _text(request, "query")
    if len(text) > MAX_QUERY_CHARS:
        raise Rejected("invalid_request", f"query must be at most {MAX_QUERY_CHARS} characters")
    projects = request.get("projects", [])
    if not isinstance(projects, list) or not all(isinstance(item, str) and item for item in projects):
        raise Rejected("invalid_request", "projects must be a list of non-empty strings")
    limit, context = request.get("limit", 10), request.get("context", 1)
    if not (_is_int(limit) and _is_int(context)):
        raise Rejected("invalid_request", "limit and context must be integers")
    group = request.get("group_sessions", False)
    if not isinstance(group, bool):
        raise Rejected("invalid_request", "group_sessions must be a boolean")
    return core.Query(domain, text, tuple(projects), limit, context, group)


def _refusal(operation: str, error: Rejected) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_id": operation,
        "state": "rejected",
        "error": {"class": error.error_class, "retry": error.retry, "detail": error.detail},
    }


def search(searcher: core.SessionSearch, grant: Grant, request: dict, embedder: Embedder | None = None) -> dict:
    operation = f"search:{digest(request)[:16]}"
    try:
        answer = searcher.search(grant, _check(request), embedder)
    except Rejected as error:
        return _refusal(operation, error)
    except core.ScopeRefused:
        return _refusal(operation, Rejected("forbidden_scope", core.SCOPE_REFUSED))
    except ValueError as error:
        return _refusal(operation, Rejected("invalid_request", str(error)))
    except sqlite3.OperationalError:
        return _refusal(operation, Rejected("dependency_unavailable", SEARCH_FAILED, retry="same_request"))
    return {"schema_version": SCHEMA_VERSION, "operation_id": operation, "state": "answered", **answer}
