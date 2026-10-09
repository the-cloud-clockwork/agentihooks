import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from scripts import session_bands
from scripts.routing import split
from scripts.routing.settings import VALIDATORS, open_store
from scripts.routing.slots import API_UNBOUNDED, Slot

if TYPE_CHECKING:
    from redis import Redis

    from scripts.routing.slots import SlotSource


class SettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApiPolicy:
    weight: int = 0
    cap: int = API_UNBOUNDED


def _client(environ: Mapping[str, str]) -> "Redis | None":
    from redis import RedisError

    from scripts.swarm import store

    try:
        return store.redis_client(environ)
    except (RedisError, OSError):
        return None


def policy(harness: str, environ: Mapping[str, str]) -> ApiPolicy:
    from redis import RedisError

    cap_key, weight_key = f"{harness}-api-max-sessions", f"{harness}-api-weight"
    try:
        settings = open_store(_client(environ), environ)
        cap = settings.get(cap_key)
        weight = settings.get(weight_key)
    except (RedisError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SettingsError(f"routing settings are unreadable: {type(exc).__name__}") from exc
    for key, value in ((cap_key, cap), (weight_key, weight)):
        if (value is not None or key == weight_key) and not VALIDATORS[key](value):
            raise SettingsError(f"routing setting {key} is invalid")
    return ApiPolicy(weight, API_UNBOUNDED if cap is None else cap)


def api_side(source: "SlotSource", harness: str, environ: Mapping[str, str], now: float) -> tuple[list[Slot], int]:
    found = source.slots(environ, now)
    if not found:
        return [], 0
    try:
        rules = policy(harness, environ)
    except SettingsError as exc:
        print(f"[{harness}] api side closed: {exc}", file=sys.stderr)
        return [], 0
    return [replace(slot, cap=rules.cap) for slot in found], rules.weight


def place(api: Sequence[Slot], pool: Sequence[Slot], weight: int, pool_live: int) -> Slot | None:
    open_pool = [slot for slot in pool if slot.free]
    api_live = sum(slot.sessions for slot in api)
    side = split.choose_side(api, open_pool, weight, api_live, pool_live, any(slot.free for slot in api))
    if side is None:
        return None
    return session_bands.pick(api if side == "api" else open_pool)
