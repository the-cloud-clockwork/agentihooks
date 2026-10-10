"""A worker's prompt hook claims its fleet broadcasts with the launch grant its launch left in the attempt's private run
folder. The hook environment names only the grant file: the settings writer drops credential shaped literals, and a
token in settings would outlive its execution. Swarm modules load only once a grant file is named, because the hook runs
on every prompt of every session."""

import base64
import binascii
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2.auth_context import Registration

GRANT_FILE = "AGENTIHOOKS_LAUNCH_GRANT_FILE"
GRANT_NAME = "launch-grant"


def grant_path(attempt: Path) -> Path:
    return attempt / "run" / GRANT_NAME


def store_grant(path: Path, token: str) -> None:
    descriptor, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token.strip())
    Path(staged).replace(path)


def read_grant(environ: Mapping[str, str]) -> str:
    return Path(environ[GRANT_FILE]).read_text(encoding="utf-8").strip()


def _claims(token: str) -> dict:
    from scripts.swarm.store import SwarmError
    from scripts.swarm_v2.auth_context import CLAIMS, TOKEN_PREFIX, _claims_valid

    parts = token.split(".")
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    except (IndexError, binascii.Error, ValueError):
        claims = None
    if parts[0] != TOKEN_PREFIX or not isinstance(claims, dict) or set(claims) != CLAIMS or not _claims_valid(claims):
        raise SwarmError("unauthenticated")
    return claims


def registered(store: "RedisStore", token: str) -> "Registration":
    """The worker holds no signing key, so a grant counts only while it is unexpired, recorded at registration under
    its grant id, and still the current attempt of its seat."""
    from scripts.swarm import lease
    from scripts.swarm.store import SwarmError
    from scripts.swarm_v2.auth_context import Registration, _seconds

    claims = _claims(token)
    if lease.now_ms(store) // 1000 >= _seconds(claims["expires_at"]):
        raise SwarmError("unauthenticated")
    raw = store.redis.hget(store.key(claims["swarm_id"], "launch-registrations"), claims["execution_id"])
    registration = Registration(**json.loads(raw)) if raw else None
    if registration is None or registration.grant_id != claims["grant_id"]:
        raise SwarmError("unauthenticated")
    occupants = store.execution_occupants(registration.swarm_id).values()
    if (registration.execution_id, registration.generation) not in {(a.execution_id, a.generation) for a in occupants}:
        raise SwarmError("stale_generation")
    return registration


def no_operator(token: str) -> str:
    from scripts.swarm.store import SwarmError

    raise SwarmError("forbidden_scope")


def claim(session_id: str, channels: list[str], environ: Mapping[str, str]) -> int:
    if not environ.get(GRANT_FILE):
        return 0
    from redis.exceptions import RedisError

    from scripts.swarm import store as swarm_store
    from scripts.swarm_v2.broadcasts import FLAG, FleetBroadcasts, sync_local

    if environ.get(FLAG) != "1":
        return 0
    try:
        token = read_grant(environ)
        store = swarm_store.connect(environ)
        fleet = FleetBroadcasts(store, _claims(token)["swarm_id"], lambda grant: registered(store, grant), no_operator)
        return sync_local(fleet, token, session_id, channels, environ)
    except (OSError, RedisError, swarm_store.SwarmError):
        return 0
