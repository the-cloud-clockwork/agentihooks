import json
import logging
import re
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

from scripts.hive.auth import HiveError

from . import resources, schemas
from .errors import APIError

WORKSPACE_RE = re.compile(r"tasks/[\w.-]+/workspace")


def body(handler: object, server: ModuleType) -> dict:
    if handler.headers.get_content_type() != "application/json":
        raise APIError(415, "content_type", "Content-Type must be application/json")
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= server.MAX_BODY:
            raise ValueError
        return server.core.loads(handler.rfile.read(length))
    except ValueError:
        raise APIError(400, "schema_invalid", "Request body must be a bounded JSON object") from None


def dispatch(handler: object, server: ModuleType) -> dict | None:
    if handler.headers.get("Host") not in server.ALLOWED_HOSTS:
        raise APIError(403, "forbidden", "Host not allowed")
    origin = handler.headers.get("Origin")
    if origin is not None and origin not in server.ALLOWED_ORIGINS:
        raise APIError(403, "forbidden", "Origin not allowed")
    parts = urlsplit(handler.path).path.removeprefix("/api/v1/").split("/")
    if parts[0] != "ledgers" or len(parts) == 1:
        return global_resource(handler, server, parts)
    slug, path = parts[1], "/".join(parts[2:]) or "metadata"
    if not server.core.SLUG_RE.fullmatch(slug) or not handler.exists(slug):
        raise APIError(404, "ledger_missing", "No such ledger")
    if path == "agent-token" and handler.command == "POST":
        return agent_token(handler, server, slug)
    principal = server.authority.principal(
        server.repository.token(slug),
        slug,
        handler.headers.get("X-Ledger-Token"),
        handler.headers.get("X-Ledger-Agent"),
    )
    if principal is None and server.authority.controller(handler.headers.get("X-Controller-Credential")):
        principal = ""
    if principal is None:
        raise APIError(403, "forbidden", "Missing or wrong ledger credential")
    if handler.command == "GET":
        query = pagination(handler.path)
        if path == "events" and handler.headers.get("Accept") == "text/event-stream":
            return events(handler, server, slug)
        if path == "swarm" or path.startswith("swarm/"):
            return resources.swarm_read(server.swarm_status(slug), path, query)
        if WORKSPACE_RE.fullmatch(path):
            return workspace(server, slug, path.split("/")[1])
        return ledger_read(server, slug, path, query)
    return ledger_operation(handler, server, slug, path, principal)


def ledger_read(server: ModuleType, slug: str, path: str, query: dict) -> dict:
    parts = path.split("/")
    if parts[0] in resources.COLLECTIONS and len(parts) in (2, 3):
        return resources.read(server.repository.read(slug, "/".join(parts[:2])), path, query)
    return resources.read(server.repository.get_document(slug), path, query)


def agent_token(handler: object, server: ModuleType, slug: str) -> dict:
    agent = handler.headers.get("X-Ledger-Agent")
    if not agent:
        raise APIError(403, "forbidden", "An agent token names its agent in X-Ledger-Agent")
    member = server.authority.hive_member(handler.headers.get("X-Hive-Credential"))
    if member is None:
        raise APIError(403, "forbidden", "Missing or wrong hive credential")
    if not server.authority.hive_agent(member, slug, agent):
        raise APIError(403, "forbidden", "Agent is not placed on this member's hive")
    try:
        token = server.authority.hive_agent_token(member, slug, agent)
    except HiveError as exc:
        raise APIError(403, "forbidden", str(exc)) from exc
    return {"data": {"agent": agent, "token": token}}


def events(handler: object, server: ModuleType, slug: str) -> None:
    from scripts.swarm_ledger.events import Expired, stream

    try:
        return stream.serve(handler, server.HUB, slug, lambda: server.stream_resources(slug))
    except Expired:
        raise APIError(410, "cursor_expired", "Cursor no longer retained; reconnect without it to reload") from None


def workspace(server: ModuleType, slug: str, task_id: str) -> dict:
    try:
        tails = server.workspace_tails(slug, task_id)
    except ValueError:
        raise APIError(404, "resource_missing", "No such task work folder") from None
    return {"data": tails, "revision": resources.revision(tails)}


def global_resource(handler: object, server: ModuleType, parts: list) -> dict:
    from . import admin, routing

    if parts == ["routing", "settings"]:
        return routing.settings(handler, server, body)
    if parts == ["layout"]:
        return admin.layout(handler, server, None if handler.command == "GET" else body(handler, server))
    if handler.command == "GET" and parts[0] in ("ledgers", "bin"):
        rows = server.home_summaries() if parts[0] == "ledgers" else server.bin_summaries()
        if len(parts) == 2:
            item = next((row for row in rows if row["slug"] == parts[1]), None)
            if item is None:
                raise APIError(404, "resource_missing", "No such summary")
            return {"data": item, "revision": resources.revision(item)}
        if len(parts) == 1:
            return resources.page(rows, resources.revision(rows), pagination(handler.path))
    if parts == ["bin", "actions"] and handler.command == "POST":
        return admin.bin_action(handler, server, body(handler, server))
    raise APIError(404, "resource_missing", "No such resource")


def ledger_operation(handler: object, server: ModuleType, slug: str, path: str, principal: str) -> dict | None:
    from . import admin, mutations

    if handler.command not in ("POST", "PUT"):
        raise APIError(405, "method_not_allowed", "Use a resource operation")
    if path in ("uploads/media", "uploads/artifacts"):
        return admin.upload(handler, server, slug, path)
    payload = body(handler, server)
    if path == "swarm/actions":
        return admin.control(server, slug, principal, payload)
    if path in ("export", "swarm/export"):
        schemas.validate({"type": "object", "maxProperties": 0}, payload)
        if path == "swarm/export":
            status = server.swarm_status(slug)
            if status is None:
                raise APIError(404, "swarm_missing", "No swarm for this ledger")
            return {"data": status}
        state = server.repository.get_document(slug)
        state["_meta"] = {key: item for key, item in state["_meta"].items() if key not in ("seeds", "api_operations")}
        return {"data": state}
    if path == "operations":
        return mutations.apply(server, slug, principal, payload)
    raise APIError(404, "resource_missing", "No such operation resource")


def handle(handler: object, server: ModuleType) -> None:
    try:
        result = dispatch(handler, server)
        if result is None:
            return None
        if not urlsplit(handler.path).path.endswith("/export"):
            resources.bounded(result)
        status = 200
    except APIError as exc:
        status, result = exc.status, exc.envelope()
    except (OSError, ValueError):
        logging.getLogger(__name__).exception("Ledger API storage failure")
        status, result = 500, APIError(500, "storage_error", "Resource could not be read or written").envelope()
    return handler.send(status, json.dumps(result, ensure_ascii=False), "application/json")


def pagination(url: str) -> dict:
    parsed = parse_qs(urlsplit(url).query, keep_blank_values=True)
    if any(len(values) != 1 for values in parsed.values()):
        raise APIError(400, "schema_invalid", "Query parameters must be unique")
    query = {key: values[0] for key, values in parsed.items()}
    if "limit" in query:
        try:
            query["limit"] = int(query["limit"])
        except ValueError:
            raise APIError(400, "schema_invalid", "Limit must be an integer") from None
    schemas.validate(schemas.PAGINATION, query)
    return query
