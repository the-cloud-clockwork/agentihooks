"""Every Doctor detector over the watched swarm: one list of findings, and a line for each detector that failed."""

import contextvars
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from functools import cache
from threading import Lock

from hooks.context import context_recycle
from scripts.doctor import (
    ci,
    ci_read,
    context_overruns,
    features,
    handoffs,
    health,
    hive_pools,
    inbox,
    master_launches,
    master_prompts,
    read,
    runtime,
    slot_census,
    spawn_read,
    spawns,
    tick_delivery,
    traces,
    traces_read,
)
from scripts.inbox import wake
from scripts.inbox.store import InboxStore
from scripts.swarm import idle, input_wait, status, timing
from scripts.swarm.health import activity
from scripts.swarm.runtime import SWARM_HOME

PR_RE = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")


def open_pulls(tasks):
    found = (PR_RE.search(t.get("pr_url") or "") for t in tasks if t.get("state") == "pr")
    return sorted({(m.group(1), int(m.group(2))) for m in found if m})


def reported(store, ledger, slug):
    state = ledger.state(slug)
    tasks, events = state.get("tasks", []), state.get("_meta", {}).get("events", [])
    return {f["id"] for f in status.findings(store, slug, store.config(slug), tasks, events)}


def _inbox(store, mail, slug, now_ms, env, items):
    return inbox.findings(items, now_ms, wake.window_ms(env), read.inbox_receivers(store, mail, slug, items))


def _traces(store, tasks, slug, now_ms, env, trace_client=None):
    started = time.monotonic()
    key = store.key(slug, "doctor-task-cost")
    previous = store.redis.get(key)
    merged = traces_read.merged(tasks)
    if previous is None:
        previous = json.dumps(merged)
        store.redis.set(key, previous)
    judged = set(json.loads(previous))
    limits = traces.Limits.from_env(env)
    get = (trace_client or traces_read.client)(env)
    record = traces_read.record(slug, tasks, [asdict(a) for a in store.agents(slug)], now_ms, get)
    record["waits"], record["waits_unread"] = traces_read.waits(
        lambda name: idle.wait_history(store.redis, slug, name),
        traces.gap_agents(record, limits),
        started,
        traces_read.Budget(),
    )
    candidates = traces.task_cost_candidates(record) - judged
    record["task_cost_candidates"] = candidates
    found = traces.findings(record, limits)
    if candidates:
        store.redis.set(key, json.dumps(sorted(judged | candidates)))
    return found


def _overruns(store, slug):
    limit = status.compact_limit(store.config(slug))
    return context_overruns.findings(
        [asdict(a) for a in store.agents(slug)],
        lambda session: context_recycle.unmarked_overrun(session, limit),
    )


def _master_prompts(store, slug, now_ms):
    masters = [asdict(a) for a in store.agents(slug) if a.lane == "master"]
    since = {a["name"]: input_wait.since(store, slug, a["name"]) for a in masters}
    return master_prompts.findings(masters, since, now_ms)


def _starts(store, slug, now_ms):
    from scripts.swarm.tick import ACKNOWLEDGING

    agents = [asdict(a) for a in store.agents(slug)]
    launches = store.redis.hgetall(store.key(slug, "launches"))
    pending = frozenset(name for name, raw in launches.items() if json.loads(raw)["state"] == ACKNOWLEDGING)
    beats = {
        a["name"]: beat["at"] for a in agents if a["hive"] and (beat := idle.heartbeat(store.redis, slug, a["name"]))
    }
    return spawns.silent_starts(agents, activity.first_events(slug), now_ms, beats, pending)


def _headroom(store, now_ms):
    from scripts.hive import headroom, registry

    hives = registry.hives(store.redis)
    readings = {hive["id"]: headroom.read(store.redis, hive["id"], now_ms) for hive in hives}
    return spawns.missing_headroom(hives, readings, now_ms)


def _once(load):
    lock = Lock()
    kept = cache(load)

    def shared():
        with lock:
            return kept()

    return shared


def _ci(store, tasks, now_ms):
    pulls = open_pulls(tasks)
    records = ci_read.pull_requests(pulls, cache=store.redis)
    return [f for record in records for f in ci.findings(record, now_ms)]


def readers(store, ledger, slug, now_ms, environ=None, home=SWARM_HOME, telemetry=True):
    """`telemetry` is False to skip the trace reader, or the client factory it reads through instead of the default."""
    env = os.environ if environ is None else environ
    mail = InboxStore(store.redis)
    items = _once(lambda: read.inbox_items(mail, slug))
    tasks = _once(lambda: ledger.tasks(slug))

    found = {
        "runtime storage": lambda: runtime.findings(now_ms, env),
        "scheduled tick": lambda: tick_delivery.read(),
        "health": lambda: health.findings(
            read.health_records(store.redis, slug), now_ms, reported=reported(store, ledger, slug)
        ),
        "inbox": lambda: _inbox(store, mail, slug, now_ms, env, items()),
        "handoff": lambda: handoffs.findings(read.handoffs(store, mail, home, slug, items())),
        "spawn": lambda: spawns.findings(spawn_read.records(store, slug, now_ms)),
        "master launch": lambda: master_launches.findings(spawn_read.master_records(store, slug)),
        "slot census": lambda: slot_census.findings(spawn_read.slot_claims(store, slug)),
        "startup": lambda: _starts(store, slug, now_ms),
        "headroom": lambda: _headroom(store, now_ms),
        "hive pool": lambda: hive_pools.read(store, slug, now_ms),
        "context overrun": lambda: _overruns(store, slug),
        "master prompt": lambda: _master_prompts(store, slug, now_ms),
        "ci": lambda: _ci(store, tasks(), now_ms),
        "dev workflow": lambda: ci.red_dev_workflows(ci_read.dev_pushes(store.config(slug).repo)),
        "feature": lambda: features.findings(),
        "trace": lambda: _traces(store, tasks(), slug, now_ms, env, None if telemetry is True else telemetry),
    }
    return found if telemetry else {name: read for name, read in found.items() if name != "trace"}


def _timed(name, detect):
    with timing.step(f"doctor.{name}"):
        return detect()


def collect(found_by):
    found, failed = [], []
    if not found_by:
        return found, failed
    with ThreadPoolExecutor(max_workers=len(found_by)) as pool:
        running = {
            name: pool.submit(contextvars.copy_context().run, _timed, name, detect) for name, detect in found_by.items()
        }
    for name, future in running.items():
        try:
            found += future.result()
        except Exception as exc:
            failed.append(f"the {name} detector failed: {type(exc).__name__}: {exc}")
    return found, failed
