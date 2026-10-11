import json
from collections.abc import Mapping
from types import ModuleType

from scripts.swarm.store import PREFIX, RedisStore, SwarmConfig, SwarmError

PROOF_ENV = "AGENTIHOOKS_CONTROL_PROOF"
PROJECTS_ENV = "AGENTIHOOKS_LAUNCH_PROJECTS"
RECORD = f"{PREFIX}:controller-proof"
CONTENT = {
    "title": "Anton launch proof",
    "overview": "One engineer Pod launches on Anton and registers with the controller.",
    "phases": [{"title": "Launch one engineer on Anton"}],
    "tasks": [
        {
            "title": "Confirm the Anton launch",
            "description": "Run agentihooks swarm progress with a short line saying the Pod started, then stop.",
            "phase": "p1",
            "lane": "eng",
        }
    ],
}


def reconcile(environ: Mapping[str, str], store: RedisStore, slug: str, ledger: ModuleType) -> str:
    recorded = store.redis.get(RECORD)
    setting = environ.get(PROOF_ENV)
    if not setting:
        return _remove(environ, store, recorded, ledger) if recorded else ""
    if recorded == slug:
        return f"controller: kept proof swarm {slug}"
    asked = json.loads(setting)
    named = {**environ, "AGENTIHOOKS_AGENT_NAME": asked["agent"], "AGENTIHOOKS_SWARM_TASK": asked["task"]}
    created = ledger.create(CONTENT, named)["slug"]
    if created != slug:
        ledger.remove(created, environ)
        raise SwarmError(f"the proof ledger is named {created} but the controller serves {slug}")
    store.redis.set(RECORD, slug)
    store.create(SwarmConfig(slug, environ[PROJECTS_ENV].split(",")[0], 1, 0))
    return f"controller: created proof swarm {slug} with one engineer task"


def _remove(environ: Mapping[str, str], store: RedisStore, slug: str, ledger: ModuleType) -> str:
    ledger.remove(slug, environ)
    if slug in store.slugs():
        store.remove(slug)
    store.redis.delete(RECORD)
    return f"controller: removed proof swarm {slug}"
