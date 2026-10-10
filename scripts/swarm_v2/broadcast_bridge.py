"""A worker's prompt hook claims its fleet broadcasts with the launch grant its launch left in the attempt's private run
folder. The hook environment names only the grant file: the settings writer drops credential shaped literals, and a
token in settings would outlive its execution. Swarm modules load only once a grant file is named, because the hook runs
on every prompt of every session."""

import json
import os
import tempfile
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
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        request = Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urlopen(request, timeout=TIMEOUT_SECONDS) as answer:
            found = json.loads(answer.read())["deliveries"]
        return [_delivery(delivery) for delivery in found]


def claim(session_id: str, channels: list[str], environ: Mapping[str, str]) -> int:
    if not environ.get(GRANT_FILE) or not environ.get(API_URL):
        return 0
    from scripts.swarm_v2.broadcasts import sync_local

    try:
        return sync_local(RemoteFleet(environ[API_URL]), read_grant(environ), session_id, channels, environ)
    except (OSError, ValueError):
        return 0
