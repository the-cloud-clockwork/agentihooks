"""Read only loaders for the Doctor rates: the ledger, tool activity, health findings, the gate log, injection traces
and the pull requests of tasks done in the span, cached in Redis."""

import json
import subprocess
from pathlib import Path

from hooks import config
from hooks.context import injection_trace
from scripts.doctor import read
from scripts.doctor.rates import Pull, Records
from scripts.swarm.health import activity
from scripts.swarm.ledger_events import iso_ms
from scripts.swarm.runtime import SWARM_HOME
from scripts.swarm.store import PREFIX

FIELDS = "state,mergedAt,additions,deletions,files"
OPEN_TTL_S = 300
SETTLED_TTL_S = 7 * 24 * 3600


def gate_log_path(slug, home=SWARM_HOME):
    return Path(home) / slug / "gates" / "log.jsonl"


def _lines(path):
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    rows = []
    for line in raw.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _timed(rows):
    found = []
    for row in rows:
        try:
            found.append({**row, "at": iso_ms(row["at"])})
        except (KeyError, TypeError, ValueError):
            continue
    return found


def injections(since_ms):
    folder = Path(config.AGENTIHOOKS_HOME) / "injections"
    rows = []
    for path in sorted(folder.glob("*.jsonl")) if folder.is_dir() else []:
        if path.stat().st_mtime * 1000 >= since_ms:
            rows += _lines(path)
    return _timed(rows)


def pull(raw):
    merged = iso_ms(raw["mergedAt"]) if raw.get("mergedAt") else None
    lines = (raw.get("additions") or 0) + (raw.get("deletions") or 0)
    return Pull(raw["state"], merged, lines, tuple(f["path"] for f in raw.get("files") or []))


def fetch(url, run=subprocess.run):
    try:
        done = run(["gh", "pr", "view", url, "--json", FIELDS], capture_output=True, text=True, timeout=20)
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def pulls(redis, slug, urls, run=subprocess.run):
    found = {}
    for url in sorted(set(urls)):
        key = ":".join((PREFIX, slug, "rates-pull", url))
        raw = redis.get(key)
        if raw is None:
            fetched = fetch(url, run)
            if not isinstance(fetched, dict) or "state" not in fetched:
                continue
            raw = json.dumps(fetched)
            redis.set(key, raw, ex=OPEN_TTL_S if fetched["state"] == "OPEN" else SETTLED_TTL_S)
        found[url] = pull(json.loads(raw))
    return found


def _done_urls(events, tasks, span):
    done = [
        e["target"].removeprefix("tasks/")
        for e in events
        if e["kind"] == "task done" and span.holds(e["at"]) and e["target"].startswith("tasks/")
    ]
    return [tasks[tid]["pr_url"] for tid in done if tasks.get(tid, {}).get("pr_url")]


def load(store, ledger, slug, span, run=subprocess.run, home=SWARM_HOME):
    state = ledger.state(slug)
    events = state.get("_meta", {}).get("events", [])
    tasks = {t["id"]: t for t in state.get("tasks", [])}
    return Records(
        events=events,
        tasks=tasks,
        activity=activity.entries(slug),
        findings=read.health_records(store.redis, slug),
        gate_log=_lines(gate_log_path(slug, home)),
        injections=injections(span.start),
        corrections=_timed(injection_trace.corrections()),
        pulls=pulls(store.redis, slug, _done_urls(events, tasks, span), run),
    )
