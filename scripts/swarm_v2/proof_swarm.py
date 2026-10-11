import json
import sys
from collections.abc import Callable, Mapping
from types import ModuleType

from scripts.swarm import naming
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError

PROOF_ENV = "AGENTIHOOKS_CONTROL_PROOF"
PROJECTS_ENV = "AGENTIHOOKS_LAUNCH_PROJECTS"
SWARM_ENV = "AGENTIHOOKS_CONTROL_SWARM"
MALFORMED = f"{PROOF_ENV} must be a JSON object naming an agent and a task"
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
            "profile": "engineer",
        }
    ],
}


def run(environ: Mapping[str, str], store: RedisStore) -> None:
    from scripts.swarm_ledger import ledger_link, proof_ledger

    if not ledger_link.remote(environ):
        return
    try:
        lines = reconcile(environ, store, environ.get(SWARM_ENV, ""), proof_ledger)
    except SwarmError as exc:
        print(
            f"controller: the proof swarm pass failed and runs again after the next tick: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return
    for line in lines:
        print(line, flush=True)


def reconcile(environ: Mapping[str, str], store: RedisStore, slug: str, ledger: ModuleType) -> list[str]:
    asked = _asked(environ)
    stale = [other for other in store.slugs() if naming.PROOF_RE.fullmatch(other) and (asked is None or other != slug)]
    lines = [_remove(environ, store, other, ledger) for other in stale]
    if asked is not None and slug not in store.slugs():
        lines.append(_create(environ, store, slug, ledger, asked))
    return lines


def _asked(environ: Mapping[str, str]) -> dict | None:
    setting = environ.get(PROOF_ENV)
    if not setting:
        return None
    try:
        asked = json.loads(setting)
    except json.JSONDecodeError:
        raise SwarmError(MALFORMED) from None
    if not isinstance(asked, dict) or not asked.get("agent") or not asked.get("task"):
        raise SwarmError(MALFORMED)
    return asked


def _create(environ: Mapping[str, str], store: RedisStore, slug: str, ledger: ModuleType, asked: dict) -> str:
    if not naming.PROOF_RE.fullmatch(slug):
        raise SwarmError(f"the controller serves {slug!r}, which is not a proof swarm name")
    repo = _repo(environ)
    found = _call(ledger.state, slug, environ)
    if found == "binned":
        raise SwarmError(f"the proof ledger {slug} is in the bin: name a new proof or restore it")
    if found == "missing":
        named = {**environ, "AGENTIHOOKS_AGENT_NAME": asked["agent"], "AGENTIHOOKS_SWARM_TASK": asked["task"]}
        created = _call(ledger.create, CONTENT, named)["slug"]
        if created != slug:
            _call(ledger.remove, created, environ)
            raise SwarmError(f"the proof ledger is named {created} but the controller serves {slug}")
    store.create(SwarmConfig(slug, repo, 1, 0))
    return f"controller: created proof swarm {slug} with one engineer task"


def _remove(environ: Mapping[str, str], store: RedisStore, slug: str, ledger: ModuleType) -> str:
    if _call(ledger.state, slug, environ) == "live":
        _call(ledger.remove, slug, environ)
    if store.agents(slug):
        return f"controller: proof swarm {slug} has its ledger removed and waits for its agents to end"
    store.remove(slug)
    return f"controller: removed proof swarm {slug}"


def _repo(environ: Mapping[str, str]) -> str:
    projects = [project.strip() for project in environ.get(PROJECTS_ENV, "").split(",") if project.strip()]
    if not projects:
        raise SwarmError(f"a proof swarm needs {PROJECTS_ENV}")
    return projects[0]


def _call(action: Callable, *args: object):
    try:
        return action(*args)
    except (SystemExit, OSError) as error:
        raise SwarmError(f"the proof ledger route failed: {error}") from None
