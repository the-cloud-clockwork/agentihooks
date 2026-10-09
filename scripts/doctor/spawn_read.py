from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING

from scripts.handoff import transfers
from scripts.swarm.store import MASTER, SwarmError

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore


def records(
    store: RedisStore,
    slug: str,
    now_ms: int,
    since: str = "1 hour ago",
    run: Callable = subprocess.run,
    *,
    until: str | None = None,
) -> dict:
    journal = run(
        [
            "journalctl",
            "--user",
            "-u",
            "agentihooks-swarm.service",
            "--since",
            since,
            *(["--until", until] if until is not None else []),
            "-o",
            "cat",
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return {
        "slug": slug,
        "now": now_ms,
        "spawns": store.spawns(slug),
        "agents": [asdict(a) for a in store.agents(slug)],
        "history": [json.loads(row) for row in store.redis.lrange(store.key(slug, "history"), 0, -1)],
        "restored": store.restored(slug),
        "actions": [
            line for line in journal.stdout.splitlines() if line.startswith(f"{slug}: ") and "spawn failed" in line
        ],
    }


def master_records(
    store: RedisStore,
    slug: str,
    since: str = "1 day ago",
    run: Callable = subprocess.run,
    *,
    until: str | None = None,
) -> dict:
    entries, error = _grep("master spawn failed", since, until, run)
    journal = None if entries is None else [e for e in entries if e["message"].startswith(f"{slug}: ")]
    return {
        "slug": slug,
        "transfers": [row for row in transfers.list_transfers(store, slug) if row["task"] == MASTER],
        "restored": [row for row in store.restored(slug) if row["lane"] == MASTER],
        "agents": [asdict(a) for a in store.agents(slug) if a.lane == MASTER],
        "journal": journal,
        "journal_error": error,
    }


def doctor_passes(doctor: str, since: str, until: str, run: Callable = subprocess.run) -> tuple[int, ...]:
    entries, error = _grep(f"{doctor}: doctor pass", since, until, run)
    if entries is None:
        raise SwarmError(f"Doctor passes unavailable: {error}")
    return tuple(e["at"] for e in entries)


def _grep(pattern, since, until, run):
    argv = [
        "journalctl",
        "--user",
        "-u",
        "agentihooks-swarm.service",
        "--since",
        since,
        *(["--until", until] if until is not None else []),
        "-o",
        "json",
        "--output-fields=MESSAGE,_PID",
        "-g",
        pattern,
        "--no-pager",
    ]
    try:
        done = run(argv, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if done.returncode and (done.stdout.strip() or done.stderr.strip()):
        return None, f"journalctl exit {done.returncode}: {done.stderr.strip()}"
    entries = (json.loads(line) for line in done.stdout.splitlines() if line.strip())
    return [
        {"at": int(e["__REALTIME_TIMESTAMP"]) // 1000, "pid": e.get("_PID", ""), "message": e["MESSAGE"]}
        for e in entries
    ], ""
