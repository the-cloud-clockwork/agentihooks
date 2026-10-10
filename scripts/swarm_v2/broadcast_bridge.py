"""A worker's prompt hook claims its fleet broadcasts with the launch grant its launch left in the attempt's private run
folder. The hook environment names only the grant file and the swarm API address: the settings writer drops credential
shaped literals, and a token in settings would outlive its execution. Swarm modules load only once both are named,
because the hook runs on every prompt of every session."""

import json
import os
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.swarm_v2.broadcasts import Delivery

GRANT_FILE = "AGENTIHOOKS_LAUNCH_GRANT_FILE"
GRANT_NAME = "launch-grant"
API_URL = "AGENTIHOOKS_SWARM_API_URL"
CLAIM_PATH = "/v2/broadcasts/claim"
TIMEOUT_SECONDS = 5
CLAIM_ATTEMPTS = 2
JSON_HEADERS = {"Content-Type": "application/json"}
AUTHORIZATION = "Authorization"


def grant_path(attempt: Path) -> Path:
    return attempt / "run" / GRANT_NAME


def store_grant(path: Path, token: str) -> None:
    descriptor, staged = tempfile.mkstemp(dir=path.parent)
    try:
        os.write(descriptor, token.strip().encode())
    finally:
        os.close(descriptor)
    Path(staged).replace(path)


def read_grant(environ: Mapping[str, str]) -> str:
    return Path(environ[GRANT_FILE]).read_bytes().decode().strip()


class RemoteFleet:
    """The swarm API holds a worker's fleet broadcasts and authenticates its launch grant on every claim, so the
    worker needs no Redis credential."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/") + CLAIM_PATH

    def claim(self, token: str, channels: list[str], claim_id: str = "") -> list["Delivery"]:
        from urllib.request import Request, urlopen

        from scripts.swarm_v2.broadcasts import _delivery

        body = {"channels": channels, **({"claim_id": claim_id} if claim_id else {})}
        request = Request(
            self.url, data=json.dumps(body).encode(), headers={**JSON_HEADERS, AUTHORIZATION: f"Bearer {token}"}
        )
        with urlopen(request, timeout=TIMEOUT_SECONDS) as answer:
            found = json.loads(answer.read())["deliveries"]
        return [_delivery(delivery) for delivery in found]


def claim(session_id: str, channels: list[str], environ: Mapping[str, str]) -> int:
    if not environ.get(GRANT_FILE) or not environ.get(API_URL):
        return 0
    from scripts.swarm_v2.broadcasts import FLAG, sync_local

    if environ.get(FLAG) != "1":
        return 0
    from urllib.error import HTTPError

    fleet, claim_id = RemoteFleet(environ[API_URL]), uuid.uuid4().hex
    for _ in range(CLAIM_ATTEMPTS):
        try:
            return sync_local(fleet, read_grant(environ), session_id, channels, environ, claim_id)
        except (HTTPError, ValueError, KeyError, TypeError):
            return 0
        except OSError:
            continue
    return 0
