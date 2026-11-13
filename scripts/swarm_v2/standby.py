"""The control API of a standby controller replica: health answered here, every other request sent to the leader."""

import http.client
import json
import re
import urllib.error
import urllib.request
from typing import Any

from scripts.swarm.store import RedisStore
from scripts.swarm_v2.api import health, readiness
from scripts.swarm_v2.auth_context import GrantRefused

LEADER_KEY = "controller-api-leader"
SCHEME = "http"
FORWARD_S = 30
STANDBY = "standby"
UNAVAILABLE = "dependency_unavailable"
NO_LEADER = "no controller replica leads the swarm yet"
UNANSWERED = "the leading controller replica did not answer"
NOT_SERVED = "invalid_request"
NO_ENDPOINT = "no such endpoint on the published port"


def publish_leader(store: RedisStore, slug: str, address: str, ttl_ms: int | None = None) -> None:
    """Record the control API address of the replica that leads ``slug``, for its standbys to forward to."""
    store.redis.set(store.key(slug, LEADER_KEY), address, px=ttl_ms)  # pyright: ignore[reportUnknownMemberType]


class Standby:
    """Answer health probes on a standby replica and forward every other control API request to the leader;
    with ``only`` set, refuse every path it does not match, as the leader's published port does."""

    def __init__(self, store: RedisStore, slug: str, only: re.Pattern[str] | None = None) -> None:
        self.store, self.slug, self.only = store, slug, only
        self.redis: Any = store.redis  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        self.readiness = readiness.Probe(self.redis)  # pyright: ignore[reportUnknownMemberType]

    def route(self, method: str, path: str, authorization: str, body: object) -> tuple[int, dict[str, Any]]:
        if self.only is not None and not self.only.fullmatch(path):
            return 404, _refused(NOT_SERVED, NO_ENDPOINT)
        if (method, path) == ("GET", health.LIVE_PATH):
            return 200, {"probe": health.SHALLOW, "status": STANDBY}
        if (method, path) == ("GET", health.READY_PATH):
            available = self.readiness.check()
            return (200 if available else 503), {"redis": health.OK if available else health.UNAVAILABLE}
        address: str | None = self.redis.get(self.store.key(self.slug, LEADER_KEY))
        if not address:
            return 503, _refused(UNAVAILABLE, NO_LEADER)
        return forward(address, method, path, authorization, body)


def forward(address: str, method: str, path: str, authorization: str, body: object) -> tuple[int, dict[str, Any]]:
    """Send one control API request to the leader at ``address`` and return its status and JSON reply."""
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": authorization, "Content-Type": "application/json"}
    request = urllib.request.Request(f"{SCHEME}://{address}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=FORWARD_S) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as refused:
        try:
            return refused.code, json.loads(refused.read())
        except (OSError, ValueError, http.client.HTTPException):
            return 503, _refused(UNAVAILABLE, UNANSWERED)
    except (OSError, ValueError, http.client.HTTPException):
        return 503, _refused(UNAVAILABLE, UNANSWERED)


def _refused(error_class: str, message: str) -> dict[str, Any]:
    return GrantRefused(error_class, message).detail()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
