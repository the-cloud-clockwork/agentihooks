"""Every Doctor detector over the watched swarm: one list of findings, and a line for each detector that failed."""

import os
import re
from dataclasses import asdict
from functools import cache

from hooks.context import context_recycle
from scripts.doctor import (
    ci,
    ci_read,
    context_overruns,
    handoffs,
    health,
    inbox,
    master_launches,
    read,
    runtime,
    spawn_read,
    spawns,
    traces,
    traces_read,
)
from scripts.inbox import wake
from scripts.inbox.store import InboxStore
from scripts.swarm import idle, status, timing
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


def _traces(store, ledger, slug, now_ms, env):
    record = traces_read.record(
        slug, ledger.tasks(slug), [asdict(a) for a in store.agents(slug)], now_ms, traces_read.client(env)
    )
    record["waits"] = {
        name: idle.wait_history(store.redis, slug, name)
        for name in {traces._agent(trace) for trace in record["traces"]}
    }
    return traces.findings(record, traces.Limits.from_env(env))


def _overruns(store, slug):
    limit = status.compact_limit(store.config(slug))
    return context_overruns.findings(
        [asdict(a) for a in store.agents(slug)],
        lambda session: context_recycle.unmarked_overrun(session, limit),
    )


def readers(store, ledger, slug, now_ms, environ=None, home=SWARM_HOME, telemetry=True):
    env = os.environ if environ is None else environ
    mail = InboxStore(store.redis)

    @cache
    def items():
        return read.inbox_items(mail, slug)

    found = {
        "runtime storage": lambda: runtime.findings(now_ms, env),
        "health": lambda: health.findings(
            read.health_records(store.redis, slug), now_ms, reported=reported(store, ledger, slug)
        ),
        "inbox": lambda: _inbox(store, mail, slug, now_ms, env, items()),
        "handoff": lambda: handoffs.findings(read.handoffs(store, mail, home, slug, items())),
        "spawn": lambda: spawns.findings(spawn_read.records(store, slug, now_ms)),
        "master launch": lambda: master_launches.findings(spawn_read.master_records(store, slug)),
        "startup": lambda: spawns.silent_starts(
            [asdict(a) for a in store.agents(slug)], activity.first_events(slug), now_ms
        ),
        "context overrun": lambda: _overruns(store, slug),
        "ci": lambda: [
            f
            for repo, number in open_pulls(ledger.tasks(slug))
            for f in ci.findings(ci_read.pull_request(repo, number, cache=store.redis), now_ms)
        ],
        "trace": lambda: _traces(store, ledger, slug, now_ms, env),
    }
    return found if telemetry else {name: read for name, read in found.items() if name != "trace"}


def collect(found_by):
    found, failed = [], []
    for name, detect in found_by.items():
        try:
            with timing.step(f"doctor.{name}"):
                found += detect()
        except Exception as exc:
            failed.append(f"the {name} detector failed: {type(exc).__name__}: {exc}")
    return found, failed
