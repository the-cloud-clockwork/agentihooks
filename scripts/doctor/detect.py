"""Every Doctor detector over the watched swarm: one list of findings, and a line for each detector that failed."""

import os
import re
from dataclasses import asdict

from scripts.doctor import ci, ci_read, handoffs, health, inbox, read, spawn_read, spawns, traces, traces_read
from scripts.inbox import wake
from scripts.inbox.store import InboxStore
from scripts.swarm.health import activity
from scripts.swarm.runtime import SWARM_HOME

PR_RE = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")


def open_pulls(tasks):
    found = (PR_RE.search(t.get("pr_url") or "") for t in tasks if t.get("state") == "pr")
    return sorted({(m.group(1), int(m.group(2))) for m in found if m})


def readers(store, ledger, slug, now_ms, environ=None, home=SWARM_HOME):
    env = os.environ if environ is None else environ
    mail = InboxStore(store.redis)
    return {
        "health": lambda: health.findings(read.health_records(store.redis, slug), now_ms),
        "inbox": lambda: inbox.findings(read.inbox_items(mail, slug), now_ms, wake.window_ms(env)),
        "handoff": lambda: handoffs.findings(read.handoffs(store, mail, home, slug)),
        "spawn": lambda: spawns.findings(spawn_read.records(store, slug)),
        "startup": lambda: spawns.silent_starts(
            [asdict(a) for a in store.agents(slug)], activity.first_events(slug), now_ms
        ),
        "ci": lambda: [
            f
            for repo, number in open_pulls(ledger.tasks(slug))
            for f in ci.findings(ci_read.pull_request(repo, number))
        ],
        "trace": lambda: traces.findings(
            traces_read.record(
                slug, ledger.tasks(slug), [asdict(a) for a in store.agents(slug)], now_ms, traces_read.client(env)
            ),
            traces.Limits.from_env(env),
        ),
    }


def collect(found_by):
    found, failed = [], []
    for name, detect in found_by.items():
        try:
            found += detect()
        except Exception as exc:
            failed.append(f"the {name} detector failed: {type(exc).__name__}: {exc}")
    return found, failed
