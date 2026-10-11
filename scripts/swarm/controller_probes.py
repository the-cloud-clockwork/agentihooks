import json
import urllib.request
from collections.abc import Callable, Mapping

from scripts.swarm import lease
from scripts.swarm.store import RedisStore

PORT_ENV = "AGENTIHOOKS_CONTROL_API_PORT"
SWARM_ENV = "AGENTIHOOKS_CONTROL_SWARM"
LIVE_TIMEOUT = 3


def live(environ: Mapping[str, str], store: RedisStore, fetch: Callable = urllib.request.urlopen) -> int:
    try:
        fetch(f"http://127.0.0.1:{environ[PORT_ENV]}/v2/health/live", timeout=LIVE_TIMEOUT)
    except OSError as error:
        print(f"controller: the health endpoint did not answer: {type(error).__name__}: {error}")
        return 1
    stale = [slug for slug in store.slugs() if not lease.current(store, slug)]
    if stale:
        print(f"controller: no live controller lease for {stale}")
        return 1
    print("controller: live")
    return 0


def ready(environ: Mapping[str, str], store: RedisStore, brain: Callable[[], str] | None = None) -> int:
    from scripts.swarm_v2.api import health

    probe = health.brain_probe(environ) if brain is None else brain
    report = health.Health(store, environ[SWARM_ENV], probe).diagnose()
    dependencies = report["dependencies"]
    print(
        json.dumps(
            {
                "probe": report["probe"],
                "status": report["status"],
                "redis": dependencies["redis"]["state"],
                "brain": dependencies["brain"]["state"],
                "remediation": [hint["class"] for hint in report["remediation"]],
            }
        )
    )
    return 0 if dependencies["redis"]["state"] == health.OK else 1
