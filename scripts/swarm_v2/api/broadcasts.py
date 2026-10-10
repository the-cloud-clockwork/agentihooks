from collections.abc import Mapping
from dataclasses import asdict
from typing import NoReturn

from redis.exceptions import RedisError

from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2.auth_context import GrantRefused, LaunchAuthority
from scripts.swarm_v2.broadcasts import NAME, FleetBroadcasts

BEARER = "Bearer "
CLAIM = "/v2/broadcasts/claim"
FIELDS = frozenset({"channels", "claim_id"})
STATUS = {
    "invalid_request": 400,
    "unauthenticated": 401,
    "forbidden_scope": 403,
    "stale_generation": 409,
    "dependency_unavailable": 503,
}
FLEET_REFUSALS = {
    "stale_generation": ("stale_generation", "a newer attempt of this seat already claimed"),
    "forbidden_scope": ("forbidden_scope", "the launch grant is outside this swarm"),
}
UNAVAILABLE = ("dependency_unavailable", "fleet broadcasts kept changing; nothing was claimed")


def no_operator(token: str) -> NoReturn:
    raise GrantRefused("forbidden_scope", "workers never publish as the operator")


class BroadcastsAPI:
    """Worker broadcast endpoints; every call authenticates its launch grant with `LaunchAuthority.bound`."""

    def __init__(self, grants: LaunchAuthority, store: RedisStore, slug: str) -> None:
        self.fleet = FleetBroadcasts(store, slug, lambda token: grants.bound(slug, token), no_operator)

    def route(self, method: str, path: str, authorization: str, body: object) -> tuple[int, dict]:
        token = authorization.removeprefix(BEARER)
        if not authorization.startswith(BEARER) or not token:
            return 401, GrantRefused("unauthenticated", "a bearer credential is required").detail()
        try:
            if (method, path) == ("POST", CLAIM):
                return 200, self.claim(token, body)
        except GrantRefused as error:
            return STATUS[error.error_class], error.detail()
        except SwarmError as error:
            refused = GrantRefused(*FLEET_REFUSALS.get(str(error), UNAVAILABLE))
            return STATUS[refused.error_class], refused.detail()
        except RedisError:
            return 503, GrantRefused("dependency_unavailable", "the broadcast store is unavailable").detail()
        return 404, GrantRefused("invalid_request", "no such broadcast endpoint").detail()

    def claim(self, token: str, body: object) -> dict:
        if not isinstance(body, Mapping) or "channels" not in body or not set(body) <= FIELDS:
            raise GrantRefused("invalid_request", "the request carries channels and an optional claim_id")
        channels, claim_id = body["channels"], body.get("claim_id", "")
        if not isinstance(channels, list) or not all(isinstance(channel, str) for channel in channels):
            raise GrantRefused("invalid_request", "channels must be a list of channel names")
        if "claim_id" in body and not (isinstance(claim_id, str) and NAME.fullmatch(claim_id)):
            raise GrantRefused("invalid_request", "claim_id must be a lowercase name")
        return {"deliveries": [asdict(found) for found in self.fleet.claim(token, channels, claim_id)]}
