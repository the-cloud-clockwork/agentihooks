import json
import sys
from collections.abc import Callable, Mapping
from types import ModuleType
from typing import TYPE_CHECKING

from scripts.swarm import naming
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2 import retention

if TYPE_CHECKING:
    from scripts.swarm_v2.kubernetes.failures import Recovery

PROOF_ENV = "AGENTIHOOKS_CONTROL_PROOF"
CANCELLED = "cancelled"
FRESH = "fresh"
FRESH_REASON = "the controller owns this proof swarm, which keeps no checkpoints and runs one trivial task"
PROJECTS_ENV = "AGENTIHOOKS_LAUNCH_PROJECTS"
SWARM_ENV = "AGENTIHOOKS_CONTROL_SWARM"
MALFORMED = f"{PROOF_ENV} must be a JSON object naming an agent and a task"
SCRATCH_ENV = "AGENTIHOOKS_CONTROL_SCRATCH_SWARMS"
HOST_SWARMS_ENV = "AGENTIHOOKS_CONTROL_HOST_SWARMS"
SCRATCH_FIELDS = ("swarm", "agent", "task", "repo")
SCRATCH_MALFORMED = f"{SCRATCH_ENV} must be a JSON list of objects naming a proof swarm, an agent, a task and a repo"
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


def run(environ: Mapping[str, str], store: RedisStore, require: Callable[[], None] | None = None) -> None:
    from scripts.swarm_ledger import ledger_link, proof_ledger

    if not ledger_link.remote(environ):
        return
    try:
        lines = reconcile(environ, store, environ.get(SWARM_ENV, ""), proof_ledger, require)
    except SwarmError as exc:
        print(
            f"controller: the proof swarm pass failed and runs again after the next tick: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return
    for line in lines:
        print(line, flush=True)


def reconcile(
    environ: Mapping[str, str],
    store: RedisStore,
    slug: str,
    ledger: ModuleType,
    require: Callable[[], None] | None = None,
) -> list[str]:
    asked = _asked(environ)
    listed = _scratch(environ)
    if asked is not None and slug in listed:
        raise SwarmError(f"{slug} is the served proof swarm and may not be listed as a scratch swarm")
    kept = set(listed) | ({slug} if asked is not None else set())
    stale = [other for other in store.slugs() if naming.PROOF_RE.fullmatch(other) and other not in kept]
    lines = [_remove(environ, store, other, ledger, require if other == slug else None) for other in stale]
    if asked is not None:
        if slug not in store.slugs():
            lines.append(_create(environ, store, slug, ledger, asked))
        _configured(environ, slug, ledger, asked, require)
    for name, entry in listed.items():
        if name not in store.slugs():
            try:
                lines.append(_create(environ, store, name, ledger, entry, entry["repo"]))
            except SwarmError as error:
                lines.append(f"controller: scratch swarm {name} is not created: {error}")
    return lines


def _scratch(environ: Mapping[str, str]) -> dict[str, dict]:
    setting = environ.get(SCRATCH_ENV)
    if not setting:
        return {}
    try:
        entries = json.loads(setting)
    except json.JSONDecodeError:
        raise SwarmError(SCRATCH_MALFORMED) from None
    if not isinstance(entries, list):
        raise SwarmError(SCRATCH_MALFORMED)
    for entry in entries:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(key), str) and entry[key] for key in SCRATCH_FIELDS
        ):
            raise SwarmError(SCRATCH_MALFORMED)
        if not naming.PROOF_RE.fullmatch(entry["swarm"]):
            raise SwarmError(SCRATCH_MALFORMED)
    listed = {entry["swarm"]: entry for entry in entries}
    if len(listed) != len(entries):
        raise SwarmError(f"{SCRATCH_ENV} lists a scratch swarm more than once")
    hosted = sorted(set(listed) & {name.strip() for name in environ.get(HOST_SWARMS_ENV, "").split(",")})
    if hosted:
        raise SwarmError(f"{', '.join(hosted)} is ticked by its own host and may not be a scratch swarm")
    return listed


def _configured(
    environ: Mapping[str, str],
    slug: str,
    ledger: ModuleType,
    asked: dict,
    require: Callable[[], None] | None,
) -> None:
    if "parent" not in asked:
        if "control" in asked:
            raise SwarmError("proof controls need an explicit requester parent")
        return
    if require is not None:
        require()
    owner = {key: asked[key] for key in ("agent", "task", "parent")}
    _call(ledger.migrate, slug, owner, environ)
    if "control" in asked:
        if require is not None:
            require()
        _call(ledger.controller_control, slug, owner, asked["control"], environ)


def choose_fresh(environ: Mapping[str, str], recovery: "Recovery") -> list[str]:
    slug = recovery.slug
    try:
        asked = _asked(environ)
    except SwarmError:
        return []
    if asked is None or environ.get(SWARM_ENV) != slug or not naming.PROOF_RE.fullmatch(slug):
        return []
    lines = []
    for execution_id in recovery.pending():
        if recovery.checkpoints.list(execution_id):
            continue
        recovery.decide(execution_id, FRESH)
        lines.append(f"controller: chose fresh for {execution_id} in proof swarm {slug}: {FRESH_REASON}")
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


def _create(
    environ: Mapping[str, str], store: RedisStore, slug: str, ledger: ModuleType, asked: dict, repo: str = ""
) -> str:
    if not naming.PROOF_RE.fullmatch(slug):
        raise SwarmError(f"the controller serves {slug!r}, which is not a proof swarm name")
    repo = repo or _repo(environ)
    found = _call(ledger.state, slug, environ)
    if found == "binned":
        raise SwarmError(f"the proof ledger {slug} is in the bin: name a new proof or restore it")
    if found == "missing":
        named = {**environ, "AGENTIHOOKS_AGENT_NAME": asked["agent"], "AGENTIHOOKS_SWARM_TASK": asked["task"]}
        created = _call(ledger.create, CONTENT, named)["slug"]
        if created != slug:
            _call(ledger.remove, created, environ)
            raise SwarmError(f"the proof ledger is named {created} but the controller serves {slug}")
    store.create(SwarmConfig(slug, repo, 1, 0, scratch=True))
    return f"controller: created proof swarm {slug} with one engineer task"


def _remove(
    environ: Mapping[str, str], store: RedisStore, slug: str, ledger: ModuleType, require: Callable[[], None] | None
) -> str:
    if _call(ledger.state, slug, environ) == "live":
        _call(ledger.remove, slug, environ)
    agents = store.agents(slug)
    if agents and require is not None:
        agents = _end(store, slug, agents, require)
    if agents:
        if require is None:
            store.update(slug, state="stopping")
        return f"controller: proof swarm {slug} has its ledger removed and waits for its agents to end"
    store.remove(slug)
    return f"controller: removed proof swarm {slug}"


def _end(store: RedisStore, slug: str, agents: list[AgentRecord], require: Callable[[], None]) -> list[AgentRecord]:
    left = []
    for agent in agents:
        execution_id = agent.execution_id
        if execution_id and not retention.retained(store, slug, execution_id):
            if retention.final(store, slug, execution_id) is None:
                end = retention.archive_watermark(store, slug, execution_id)
                retention.finalize(store, slug, require, execution_id, CANCELLED, end, f"proof/{slug}/removed")
            left.append(agent)
            continue
        store.release(slug, agent.task, agent.name)
        store.drop_agent(slug, agent.name)
    return left


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
