"""A swarm worker whose task claim no longer names it ends at its next tool call: it leaves a work note, starts its own
swarm exit and is denied the call. The claim is the Redis key the tick and hub push, read at most once per window."""

import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hooks.context import worker_mode
from scripts.swarm.keyspace import ROOT

WORKERS = frozenset({"eng", "ci"})
STOP = (
    "STOP: task {task} on swarm {slug} is no longer claimed by you; its claim names {owner}. This session is ending: "
    "run no other tool and end your turn now."
)
NOTE = "{at} {name} ended at its next tool call: the task claim names {owner}\n"
CACHE_S = 30
READ_S = 0.05
CACHE = "claim-watch.json"


def key(slug: str, task: str) -> str:
    """The Redis key holding the agent the swarm counts as the task's claimant."""
    return f"{ROOT}:swarm:{slug}:claim:{task}"


def _client(environ: Mapping[str, str]) -> Any:
    import redis

    from scripts.swarm import store

    url = store.redis_url(environ)
    return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=READ_S, socket_timeout=READ_S)


def _cached(path: Path, name: str, now: float) -> tuple[bool, str | None]:
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        return False, None
    at = saved.get("at") if isinstance(saved, dict) else None
    if not isinstance(at, int | float) or saved.get("name") != name or not 0 <= now - at < CACHE_S:
        return False, None
    return True, saved.get("holder")


def _holder(environ: Mapping[str, str], redis: Any, slug: str, task: str) -> str | None:
    import redis as redis_lib

    try:
        client = _client(environ) if redis is None else redis
        return None if client is None else client.get(key(slug, task))
    except (redis_lib.RedisError, OSError):
        return None


def check(
    environ: Mapping[str, str] | None = None,
    redis: Any = None,
    now: float | None = None,
    spawn: Callable[..., Any] | None = None,
) -> str | None:
    """The stop message for a worker whose pushed task claim names another agent, else None."""
    from scripts.swarm_ledger import ledger_workspace

    env = os.environ if environ is None else environ
    slug, name, task = (env.get(k) for k in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM_TASK"))
    if env.get("AGENTIHOOKS_SWARM_LANE") not in WORKERS or not (slug and name and task) or worker_mode.active(env):
        return None
    now = time.time() if now is None else now
    folder = ledger_workspace.folder(slug, task)
    folder.mkdir(parents=True, exist_ok=True)
    fresh, holder = _cached(folder / CACHE, name, now)
    if not fresh:
        holder = _holder(env, redis, slug, task)
        (folder / CACHE).write_text(json.dumps({"name": name, "at": now, "holder": holder}))
    if not holder or holder == name:
        return None
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    with (folder / "progress.md").open("a") as progress:
        progress.write(NOTE.format(at=at, name=name, owner=holder))
    (spawn or subprocess.Popen)(
        ["agentihooks", "swarm", slug, "--as", name, "exit"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return STOP.format(task=task, slug=slug, owner=holder)
