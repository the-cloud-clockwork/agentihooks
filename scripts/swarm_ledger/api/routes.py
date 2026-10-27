import io
import json
import logging
import os
import re
from urllib.parse import parse_qs, unquote, urlsplit

from scripts.swarm_ledger import ledger_closed, ports

from ..repository.rows import children, encode
from . import resources, schemas, task_list
from .errors import APIError
from .services import Services

WORKSPACE_RE = re.compile(r"tasks/[\w.-]+/workspace")
FILE_RE = re.compile(r"files/(media|artifacts)/[\w.-]+")
CONTEXT_RE = re.compile(r"swarm/context/[\w.-]+/[\w.:-]+")


def body(handler: object, services: Services) -> dict:
    if handler.headers.get_content_type() != "application/json":
        raise APIError(415, "content_type", "Content-Type must be application/json")
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= services.policy.max_body:
            raise ValueError
        return services.ledger.core.loads(handler.rfile.read(length))
    except ValueError:
        raise APIError(400, "schema_invalid", "Request body must be a bounded JSON object") from None


def dispatch(handler: object, services: Services) -> dict | str | None:
    if handler.headers.get("Host") not in services.policy.allowed_hosts:
        raise APIError(403, "forbidden", "Host not allowed")
    origin = handler.headers.get("Origin")
    if origin is not None and origin not in services.policy.allowed_origins:
        raise APIError(403, "forbidden", "Origin not allowed")
    parts = urlsplit(handler.path).path.removeprefix("/api/v1/").split("/")
    if parts[0] != "ledgers" or len(parts) == 1:
        return global_resource(handler, services, parts)
    slug, path = parts[1], "/".join(parts[2:]) or "metadata"
    if not services.ledger.core.SLUG_RE.fullmatch(slug) or not handler.exists(slug):
        raise APIError(404, "ledger_missing", "No such ledger")
    if path == "agent-token" and handler.command == "POST":
        return agent_token(handler, services, slug)
    if handler.command == "GET" and FILE_RE.fullmatch(path):
        _, kind, name = path.split("/")
        media = services.media
        return handler.send_media(slug, name, media.artifact_store if kind == "artifacts" else media.media_store)
    principal = services.ledger.authority.caller(handler.headers, services.repository.token(slug), slug)
    if principal is None and services.ledger.authority.controller(handler.headers.get("X-Controller-Credential")):
        principal = ""
    if principal is None:
        raise APIError(403, "forbidden", "Missing or wrong ledger credential")
    if handler.command == "GET":
        return ledger_get(handler, services, slug, path)
    return ledger_operation(handler, services, slug, path, principal)


def ledger_get(handler: object, services: Services, slug: str, path: str) -> dict | None:
    query = pagination(handler.path)
    if ("state" in query or "offset" in query or "q" in query) and path not in ledger_closed.COLLECTIONS:
        raise APIError(400, "schema_invalid", resources.DONE_ONLY)
    if path == "events" and handler.headers.get("Accept") == "text/event-stream":
        return events(handler, services, slug)
    if path == "swarm/models":
        catalog = services.swarm.models()
        return {"data": catalog, "revision": resources.revision(catalog)}
    if CONTEXT_RE.fullmatch(unquote(path)):
        return context_evidence(services, slug, *unquote(path).split("/")[2:])
    if path == "swarm" or path.startswith("swarm/"):
        return resources.swarm_read(services.swarm.status(slug), path, query)
    if WORKSPACE_RE.fullmatch(path):
        return workspace(services, slug, path.split("/")[1])
    return ledger_read(services, slug, path, query)


def ledger_read(services: Services, slug: str, path: str, query: dict) -> dict:
    if path == "controller-lease":
        from . import controller_lease

        return controller_lease.read(slug)
    if path.split("/")[0] == "hierarchy":
        return resources.hierarchy_read(services.repository, slug, path, query)
    if path == "architecture":
        from . import architecture

        return architecture.view(os.environ)
    if path == "tasks" and not query.keys() & {"state", "offset", "q"}:
        return task_list.read(services.repository, slug, query)
    entry = services.repository.current(slug)
    return resources.read(entry.state, path, query, entry.views)


def context_evidence(services: Services, slug: str, task: str, ref: str) -> dict:
    context_view = ports.swarm().context_view
    if not context_view.enabled(os.environ):
        raise APIError(404, "resource_missing", context_view.OFF)
    try:
        return {"data": context_view.expand(services.swarm.store(), slug, task, ref)}
    except context_view.Refused as refused:
        raise APIError(refused.status, refused.reason, str(refused)) from None


def agent_token(handler: object, services: Services, slug: str) -> dict:
    agent = handler.headers.get("X-Ledger-Agent")
    if not agent:
        raise APIError(403, "forbidden", "An agent token names its agent in X-Ledger-Agent")
    member = services.ledger.authority.hive_member(handler.headers.get("X-Hive-Credential"))
    if member is None:
        raise APIError(403, "forbidden", "Missing or wrong hive credential")
    if not services.ledger.authority.hive_agent(member, slug, agent):
        raise APIError(403, "forbidden", "Agent is not placed on this member's hive")
    try:
        token = services.ledger.authority.hive_agent_token(member, slug, agent)
    except ports.hive().auth.HiveError as exc:
        raise APIError(403, "forbidden", str(exc)) from exc
    return {"data": {"agent": agent, "token": token}}


def events(handler: object, services: Services, slug: str) -> None:
    from scripts.swarm_ledger.events import Expired, stream

    try:
        return stream.serve(handler, services.ledger.hub, slug, lambda: services.ledger.stream_resources(slug))
    except Expired:
        raise APIError(410, "cursor_expired", "Cursor no longer retained; reconnect without it to reload") from None


def workspace(services: Services, slug: str, task_id: str) -> dict:
    try:
        tails = services.ledger.workspace_tails(slug, task_id)
    except ValueError:
        raise APIError(404, "resource_missing", "No such task work folder") from None
    return {"data": tails, "revision": resources.revision(tails)}


def global_resource(handler: object, services: Services, parts: list) -> dict:
    from . import admin, routing

    if parts[0] == "migrations":
        from scripts.swarm_ledger.storage_migration import hosted

        return hosted.route(handler, services, parts)
    if parts == ["routing", "settings"]:
        return routing.settings(handler, services, body)
    if parts[0] == "hives":
        from . import hives

        return hives.route(handler, services, parts, body)
    if parts == ["tips"] and handler.command == "GET":
        from scripts.swarm_ledger import page_tips

        return {"data": page_tips.values()}
    if parts == ["layout"]:
        return admin.layout(handler, services, None if handler.command == "GET" else body(handler, services))
    if handler.command == "GET" and parts[0] in ("ledgers", "bin"):
        rows = services.ledger.home_summaries() if parts[0] == "ledgers" else services.ledger.bin_summaries()
        if len(parts) == 2:
            item = next((row for row in rows if row["slug"] == parts[1]), None)
            if item is None:
                raise APIError(404, "resource_missing", "No such summary")
            return {"data": item, "revision": resources.revision(item)}
        if len(parts) == 1:
            return resources.page(rows, resources.revision(rows), pagination(handler.path))
    if parts == ["bin", "actions"] and handler.command == "POST":
        return admin.bin_action(handler, services, body(handler, services))
    if parts[0] == "proof-ledgers" and handler.command == "POST":
        from . import proofs

        return proofs.route(handler, services, parts, body)
    raise APIError(404, "resource_missing", "No such resource")


def ledger_operation(handler: object, services: Services, slug: str, path: str, principal: str) -> dict | str | None:
    from . import admin, mutations

    if handler.command not in ("POST", "PUT"):
        raise APIError(405, "method_not_allowed", "Use a resource operation")
    if path in ("uploads/media", "uploads/artifacts"):
        return admin.upload(handler, services, slug, path)
    if handler.command == "PUT" and path in PAGE_WRITES:
        return getattr(handler, PAGE_WRITES[path])(slug)
    payload = body(handler, services)
    if path == "controller-lease/actions":
        from . import controller_lease

        return controller_lease.act(slug, principal, payload)
    if path == "lifecycle/actions":
        return admin.lifecycle(services, principal, slug, payload)
    if path == "swarm/actions":
        return admin.control(services, slug, principal, payload)
    if path in ("export", "swarm/export"):
        return export(handler, services, slug, path, payload)
    if path == "operations":
        return mutations.apply(services, slug, principal, payload)
    if path == "architecture/actions":
        from . import architecture

        return architecture.act(handler, services, slug, principal, payload, os.environ)
    raise APIError(404, "resource_missing", "No such operation resource")


PAGE_WRITES = {"saves": "put_ledger", "swarm/controls": "put_swarm"}


def export(handler: object, services: Services, slug: str, path: str, payload: dict) -> dict | None:
    if path == "export" and isinstance(payload, dict) and "since" in payload:
        schemas.validate(SINCE_SCHEMA, payload)
        generation, body = services.repository.export_since(slug, payload["since"], export_bytes, export_changes)
        body = body if isinstance(body, bytes) else body.encode()
        return handler.send_export(b'{"generation":' + str(generation).encode() + b"," + body[1:])
    schemas.validate({"type": "object", "maxProperties": 0}, payload)
    if path == "swarm/export":
        status = services.swarm.status(slug)
        if status is None:
            raise APIError(404, "swarm_missing", "No swarm for this ledger")
        return {"data": status}
    return handler.send_export(services.repository.encoded_view(slug, export_bytes))


def export_bytes(state: dict) -> bytes:
    meta = {key: item for key, item in state["_meta"].items() if key not in ("seeds", "api_operations")}
    with io.BytesIO() as stream:
        for chunk in json.JSONEncoder(ensure_ascii=False).iterencode({"data": {**state, "_meta": meta}}):
            stream.write(chunk.encode())
        return stream.getvalue()


SINCE_SCHEMA = {
    "type": "object",
    "properties": {"since": {"type": "integer", "minimum": 0}},
    "required": ["since"],
    "additionalProperties": False,
}
HIDDEN_META = ("seeds", "api_operations")
HIDDEN_PATHS = [[key] for key in HIDDEN_META]


def change_unit(path: str) -> tuple | None:
    parts = json.loads(path)
    if parts[:1] != ["_meta"]:
        return tuple(encode(part) for part in parts[:2])
    if parts[1:2] in HIDDEN_PATHS:
        return None
    return tuple(encode(part) for part in parts[: 3 if parts[1:2] == ["stamps"] else 2])


def identities(items: list) -> list:
    return [encode(parts[-1]) for _, parts, *_ in children(items, [], None)]


def export_changes(state: dict, paths: set) -> str | None:
    """The export items whose rows changed: dictionary entries set or dropped, and for each list holding a
    changed item its item identities in order with every changed item."""
    view = {**state, "_meta": {key: item for key, item in state["_meta"].items() if key not in HIDDEN_META}}
    units = {unit for unit in map(change_unit, paths) if unit is not None} | {(encode("_meta"), encode("events"))}
    if () in units:
        return None
    kept = [unit for unit in units if not any(unit[:depth] in units for depth in range(1, len(unit)))]
    changes = {"set": [], "drop": [], "lists": {}}
    for unit in sorted(kept):
        path, parent = [json.loads(part) for part in unit], view
        for part in path[:-1]:
            parent = parent[part]
        if isinstance(parent, list):
            items = dict(zip(identities(parent), parent))
            touched = changes["lists"].setdefault(path[0], {"order": list(items), "items": {}})
            _, identity = unit
            if identity in items:
                touched["items"][identity] = items[identity]
        elif path[-1] in parent:
            changes["set"].append([path, parent[path[-1]]])
        else:
            changes["drop"].append(path)
    return json.dumps({"changes": changes})


def handle(handler: object, services: Services) -> None:
    try:
        result = dispatch(handler, services)
        if result is None:
            return None
        if isinstance(result, task_list.Page):
            return handler.send(200, result.text(), "application/json")
        if not urlsplit(handler.path).path.endswith("/export"):
            resources.bounded(result)
        status = 200
    except APIError as exc:
        status, result = exc.status, exc.envelope()
    except (OSError, ValueError):
        logging.getLogger(__name__).exception("Ledger API storage failure")
        status, result = 500, APIError(500, "storage_error", "Resource could not be read or written").envelope()
    encoded = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return handler.send(status, encoded, "application/json")


def pagination(url: str) -> dict:
    parsed = parse_qs(urlsplit(url).query, keep_blank_values=True)
    if any(len(values) != 1 for values in parsed.values()):
        raise APIError(400, "schema_invalid", "Query parameters must be unique")
    query = {key: values[0] for key, values in parsed.items()}
    for key in ("limit", "offset"):
        if key in query:
            try:
                query[key] = int(query[key])
            except ValueError:
                raise APIError(400, "schema_invalid", f"{key.capitalize()} must be an integer") from None
    schemas.validate(schemas.PAGINATION, query)
    return query
