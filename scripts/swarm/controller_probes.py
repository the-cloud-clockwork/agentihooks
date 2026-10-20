import json
import urllib.request
from collections.abc import Callable, Mapping

from scripts.swarm import lease
from scripts.swarm.store import RedisStore

PORT_ENV = "AGENTIHOOKS_CONTROL_API_PORT"
SWARM_ENV = "AGENTIHOOKS_CONTROL_SWARM"
HOST_SWARMS_ENV = "AGENTIHOOKS_CONTROL_HOST_SWARMS"
LIVE_TIMEOUT = 3


def ticked(slugs: list[str], host_swarms: str) -> list[str]:
    left = set(host_swarms.split(","))
    return [slug for slug in slugs if slug not in left]


def live(
    environ: Mapping[str, str],
    store: RedisStore,
    fetch: Callable = urllib.request.urlopen,
    owner: str | None = None,
) -> int:
    held = ticked(store.slugs(), environ.get(HOST_SWARMS_ENV, ""))
    stale = [slug for slug in held if not lease.current(store, slug)]
    if stale:
        print(f"controller: no live controller lease for {stale}")
        return 1
    error = _unanswered(environ, fetch)
    if error:
        return _standby(environ[SWARM_ENV], store, owner, error)
    print("controller: live")
    return 0


def _unanswered(environ: Mapping[str, str], fetch: Callable) -> str:
    try:
        fetch(f"http://127.0.0.1:{environ[PORT_ENV]}/v2/health/live", timeout=LIVE_TIMEOUT)
    except OSError as error:
        return f"{type(error).__name__}: {error}"
    return ""


def _standby(slug: str, store: RedisStore, owner: str | None, error: str) -> int:
    from scripts.swarm import commands

    held = lease.current(store, slug)
    if held is None or held.owner == (owner or commands.hive_id()):
        print(f"controller: the health endpoint did not answer: {error}")
        return 1
    print(f"controller: standby while {held.owner} holds the {slug} lease")
    return 0


def ready(environ: Mapping[str, str], store: RedisStore, fetch: Callable = urllib.request.urlopen) -> int:
    from scripts.swarm_v2.api import health

    error = _unanswered(environ, fetch)
    if error:
        print(f"controller: the control API does not answer: {error}")
        return 1
    report = health.Health(store, environ[SWARM_ENV]).diagnose()
    redis = report["dependencies"]["redis"]["state"]
    print(
        json.dumps(
            {
                "probe": report["probe"],
                "status": report["status"],
                "redis": redis,
                "remediation": [{"class": hint["class"], "hint": hint["hint"]} for hint in report["remediation"]],
            }
        )
    )
    return 0 if redis == health.OK else 1
