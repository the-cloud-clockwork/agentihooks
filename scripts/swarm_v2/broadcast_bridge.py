"""A worker's prompt hook claims its fleet broadcasts with the launch grant its launch left in the attempt's private run
folder. The hook environment names only the grant file: the settings writer drops credential shaped literals, and a
token in settings would outlive its execution."""

import base64
import binascii
import json
import os
from collections.abc import Mapping
from pathlib import Path

from scripts.swarm.store import RedisStore, SwarmError, connect
from scripts.swarm_v2.auth_context import TOKEN_PREFIX, Registration
from scripts.swarm_v2.broadcasts import FLAG, FleetBroadcasts, sync_local

GRANT_FILE = "AGENTIHOOKS_LAUNCH_GRANT_FILE"
GRANT_NAME = "launch-grant"


def grant_path(attempt: Path) -> Path:
    return attempt / "run" / GRANT_NAME


def store_grant(path: Path, token: str) -> None:
    staged = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token.strip())
    staged.replace(path)


def read_grant(environ: Mapping[str, str]) -> str:
    return Path(environ[GRANT_FILE]).read_text(encoding="utf-8").strip()


def _claims(token: str) -> dict:
    parts = token.split(".")
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    except (IndexError, binascii.Error, ValueError):
        claims = None
    if parts[0] != TOKEN_PREFIX or not isinstance(claims, dict):
        raise SwarmError("unauthenticated")
    return claims


def registered(store: RedisStore, token: str) -> Registration:
    """The registration recorded for this grant; the worker holds no signing key, so the grant is trusted only as far
    as the fleet recorded it when the worker registered."""
    claims = _claims(token)
    raw = store.redis.hget(
        store.key(str(claims.get("swarm_id")), "launch-registrations"), str(claims.get("execution_id"))
    )
    registration = Registration(**json.loads(raw)) if raw else None
    if registration is None or registration.grant_id != claims.get("grant_id"):
        raise SwarmError("unauthenticated")
    return registration


def no_operator(token: str) -> str:
    raise SwarmError("forbidden_scope")


def claim(session_id: str, channels: list[str], environ: Mapping[str, str]) -> int:
    """Claim this worker's fleet broadcasts into the local cache; nothing while the fleet path is off, the grant is
    missing, or the fleet is unreachable or refuses the grant."""
    if environ.get(FLAG) != "1" or not environ.get(GRANT_FILE):
        return 0
    from redis.exceptions import RedisError

    try:
        token = read_grant(environ)
        store = connect(environ)
        fleet = FleetBroadcasts(store, _claims(token)["swarm_id"], lambda grant: registered(store, grant), no_operator)
        return sync_local(fleet, token, session_id, channels, environ)
    except (OSError, RedisError, SwarmError):
        return 0
