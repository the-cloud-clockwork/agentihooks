"""GitHub run reads shared by every process that polls them, and a per caller count of the requests GitHub charged.

A running run is served from the shared cache for RUN_TTL_S; a completed run is never cached, so the read that ends
a wait is always fresh. Entries are keyed by the credential source, named by variable, never by its value.
"""

import hashlib
import json
import os
from collections import Counter
from pathlib import Path

from scripts.swarm.lost_runner import home as _agentihooks_home

RUN_TTL_S = 60
TERMINAL = "completed"


def home() -> Path:
    return _agentihooks_home() / "github-reads"


def source() -> str:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return name
    return f"gh config {os.environ.get('GH_CONFIG_DIR') or 'default'}"


def run(endpoint, now, fetch):
    key = hashlib.sha256(f"{source()} {endpoint}".encode()).hexdigest()
    entry = home() / f"{key}.json"
    try:
        cached = json.loads(entry.read_text())
        if now - cached["at"] < RUN_TTL_S:
            return cached["run"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    found = fetch()
    if found.get("status") == TERMINAL:
        entry.unlink(missing_ok=True)
    else:
        _write(entry, {"at": now, "run": found})
    return found


def _write(path, body):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f"{path.name}.{os.getpid()}")
        staged.write_text(json.dumps(body))
        staged.replace(path)
    except OSError:
        pass


def charge(caller, endpoint, now):
    try:
        home().mkdir(parents=True, exist_ok=True)
        with (home() / "charged.jsonl").open("a") as log:
            log.write(json.dumps({"at": now, "caller": caller, "source": source(), "endpoint": endpoint}) + "\n")
    except OSError:
        pass


def counts(since, until) -> dict[str, int]:
    """Charged requests per caller with since <= at < until."""
    try:
        lines = (home() / "charged.jsonl").read_text().splitlines()
    except OSError:
        return {}
    tally = Counter()
    for line in lines:
        try:
            row = json.loads(line)
            if since <= row["at"] < until:
                tally[row["caller"]] += 1
        except (ValueError, KeyError, TypeError):
            continue
    return dict(tally)
