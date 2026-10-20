import os
import time

from scripts.routing.settings import VALIDATORS, SettingsStore

from . import resources, schemas
from .errors import APIError
from .services import BodyReader, Services

PATCH = {
    "type": "object",
    "minProperties": 1,
    "additionalProperties": False,
    "properties": {key: {} for key in VALIDATORS},
}


def store() -> SettingsStore:
    from scripts.routing import place
    from scripts.routing.settings import open_store

    return open_store(place._client(os.environ), os.environ)


def _principal(handler: object, services: Services) -> str | None:
    slug = handler.headers.get("X-Ledger-Slug") or ""
    if not services.ledger.core.SLUG_RE.fullmatch(slug) or not handler.exists(slug):
        return None
    return services.ledger.authority.caller(handler.headers, services.repository.token(slug), slug)


def _write(settings: SettingsStore, payload: dict) -> None:
    schemas.validate(PATCH, payload)
    for key, value in payload.items():
        if value is not None and not VALIDATORS[key](value):
            raise APIError(400, "schema_invalid", f"Invalid value for {key}")
    now = time.time()
    for key, value in payload.items():
        settings.set(key, value, "operator", now)


def settings(handler: object, services: Services, read_body: BodyReader) -> dict:
    if handler.command not in ("GET", "PATCH"):
        raise APIError(405, "method_not_allowed", "Use GET or PATCH")
    current = store()
    if handler.command == "PATCH":
        principal = _principal(handler, services)
        if principal is None:
            raise APIError(403, "forbidden", "Missing or wrong ledger credential")
        if principal:
            raise APIError(403, "forbidden", "Routing settings need the operator")
        _write(current, read_body(handler, services))
    data = current.all()
    return {"data": data, "revision": resources.revision(data)}
