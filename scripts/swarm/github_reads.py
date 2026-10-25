"""GitHub run reads shared by every process that polls them, and a per caller count of the requests GitHub charged.

A running run is served from the shared cache for RUN_TTL_S, so a wait sees completion up to that late; a completed
run is never cached, so every decision on a finished run is read fresh. Entries are keyed by a digest of the
credential, so one token is never served a run another token read; only that digest reaches disk, never the value.
Without a token variable the credential is the gh config folder, so a gh auth switch inside RUN_TTL_S still serves
the previous login's entry.
"""

import fcntl
import hashlib
import json
import os
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from scripts.swarm.lost_runner import home as _agentihooks_home

RUN_TTL_S = 60
TERMINAL = "completed"
TOKENS = ("GH_TOKEN", "GITHUB_TOKEN")


def home() -> Path:
    return _agentihooks_home() / "github-reads"


def source() -> str:
    for name in TOKENS:
        if os.environ.get(name):
            return name
    return f"gh config {os.environ.get('GH_CONFIG_DIR') or 'default'}"


def _key(endpoint):
    label = source()
    credential = os.environ[label] if label in TOKENS else label
    return hashlib.sha256(f"{credential} {endpoint}".encode()).hexdigest()


def run(endpoint: str, now: float, fetch: Callable[[], dict]) -> dict:
    entry = home() / f"{_key(endpoint)}.json"
    try:
        cached = json.loads(entry.read_text())
        if now - cached["at"] < RUN_TTL_S:
            return cached["run"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    found = fetch()
    if found.get("status") != TERMINAL:
        _write(entry, {"at": now, "run": found})
    return found


def _log_key(endpoint):
    if source() in TOKENS:
        return _key(endpoint)
    host = os.environ.get("GH_HOST") or "github.com"
    account = subprocess.run(
        ["gh", "config", "get", "user", "--host", host],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    ).stdout.strip()
    if not account:
        raise ValueError("GitHub account identity is unavailable")
    return _key(f"{host} {account} {endpoint}")


def job_log(endpoint: str, fetch: Callable[[], str]) -> str:
    entry = home() / f"{_log_key(endpoint)}.log"
    entry.parent.mkdir(parents=True, exist_ok=True)
    with entry.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return entry.read_text()
        except FileNotFoundError:
            pass
        found = fetch()
        staged = entry.with_name(f"{entry.name}.{os.getpid()}")
        staged.write_text(found)
        staged.replace(entry)
        return found


def snapshot(endpoint: str, now: float, fetch: Callable[[], dict]) -> dict:
    entry = home() / f"{_key(f'snapshot {endpoint}')}.json"
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        lock = entry.with_suffix(".lock").open("a")
    except OSError:
        return fetch()
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
        except OSError:
            return fetch()
        try:
            cached = json.loads(entry.read_text())
            if now - cached["at"] < RUN_TTL_S:
                return cached["snapshot"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        found = fetch()
        if found and all(value is not None for value in found.values()):
            _write(entry, {"at": now, "snapshot": found})
        return found


def _write(path, body):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_name(f"{path.name}.{os.getpid()}")
        staged.write_text(json.dumps(body))
        staged.replace(path)
    except OSError:
        pass


def charge(caller: str, endpoint: str, now: float) -> None:
    try:
        home().mkdir(parents=True, exist_ok=True)
        with (home() / "charged.jsonl").open("a") as log:
            log.write(json.dumps({"at": now, "caller": caller, "source": source(), "endpoint": endpoint}) + "\n")
    except OSError:
        pass


def counts(since: float, until: float) -> dict[str, int]:
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
