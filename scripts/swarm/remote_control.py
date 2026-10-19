"""Remote Control for interactive Claude masters: launch flags, the status line, and recorded failure notices."""

import json
import os
import time
from collections.abc import Mapping

from scripts.swarm.store import MASTER, RedisStore, connect

FAILURES = "remote-control-failures"
FALLBACK = "fallback auth"
GATEWAY = "gateway"
OFF = "flag"
FAILURES_SAID = (
    "Remote Control failed",
    "Remote Control disconnected",
    "Remote Control stopped",
    "Remote Control session expired",
)
RECOVERY = "Remote Control back on"


def now_ms() -> int:
    return int(time.time() * 1000)


def name_prefix() -> str:
    from scripts.swarm.commands import hive_id

    return hive_id()


def off_reason(route_kind: str, login: bool) -> str:
    if not login:
        return OFF
    if route_kind == "api":
        return GATEWAY
    return "" if route_kind == "interactive" else FALLBACK


def decision(lane: str, harness: str, route_kind: str, slug: str, login: bool) -> dict:
    if lane != MASTER or harness != "claude":
        return {}
    reason = off_reason(route_kind, login)
    if reason:
        return {"state": "off", "detail": reason}
    from scripts.swarm_v2 import front_door, masters

    return {"state": "on", "detail": masters.title(slug, front_door.seat(slug))}


def arguments(decided: Mapping) -> list[str]:
    if decided.get("state") != "on":
        return []
    return ["--remote-control", decided["detail"], "--remote-control-session-name-prefix", name_prefix()]


def line(agent: Mapping) -> str:
    decided = agent.get("remote_control")
    return f"Remote Control: {decided['state']} ({decided['detail']})" if decided else ""


def failure(message: str) -> bool:
    return any(said in message for said in FAILURES_SAID)


def record(store: RedisStore, slug: str, name: str, message: str, at: int) -> None:
    store.redis.hset(store.key(slug, FAILURES), name, json.dumps({"message": message, "at": at}))


def failures(store: RedisStore, slug: str) -> dict[str, dict]:
    return {name: json.loads(raw) for name, raw in store.redis.hgetall(store.key(slug, FAILURES)).items()}


def on_notification(payload: Mapping, environ: Mapping[str, str] = os.environ) -> bool:
    slug, name = environ.get("AGENTIHOOKS_SWARM"), environ.get("AGENTIHOOKS_AGENT_NAME")
    message = str(payload.get("message"))
    recovered = RECOVERY in message
    if not slug or not name or environ.get("AGENTIHOOKS_SWARM_LANE") != MASTER or not (recovered or failure(message)):
        return False
    store = RedisStore(connect())
    if recovered:
        store.redis.hdel(store.key(slug, FAILURES), name)
    else:
        record(store, slug, name, message, now_ms())
    return True
