import json
import urllib.request
from collections.abc import Callable, Mapping

from scripts.swarm import lease, tick_heartbeat
from scripts.swarm.store import RedisStore

PORT_ENV = "AGENTIHOOKS_CONTROL_API_PORT"
SWARM_ENV = "AGENTIHOOKS_CONTROL_SWARM"
LIVE_TIMEOUT = 3


def live(
    environ: Mapping[str, str],
    store: RedisStore,
    fetch: Callable = urllib.request.urlopen,
    owner: str | None = None,
    age_ms: Callable[[], int] | None = None,
) -> int:
    stale = [slug for slug in store.slugs() if not lease.current(store, slug)]
    if stale:
        print(f"controller: no live controller lease for {stale}")
        return 1
    if frozen := _frozen(store, owner, age_ms or tick_heartbeat.process_age_ms):
        print(f"controller: the tick is stale, {frozen}")
        return 1
    error = _unanswered(environ, fetch) if PORT_ENV in environ else ""
    if error:
        return _standby(environ[SWARM_ENV], store, owner, error)
    print("controller: live")
    return 0


def _frozen(store: RedisStore, owner: str | None, age_ms: Callable[[], int]) -> str:
    """A restart cannot end a ledger outage, so a swarm in one is left to the stale watcher."""
    from scripts.swarm import commands, outage

    now, mine = lease.now_ms(store), owner or commands.hive_id()
    since = now - age_ms()
    for slug in store.slugs():
        held = lease.current(store, slug)
        if held is None or held.owner != mine or store.config(slug).state not in tick_heartbeat.JUDGED:
            continue
        if any(record["kind"] == "ledger" for record in outage.open_records(slug)):
            continue
        if evidence := tick_heartbeat.stale(tick_heartbeat.read(store, slug), now, since):
            return f"{slug}: {'; '.join(evidence)}"
    return ""


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
