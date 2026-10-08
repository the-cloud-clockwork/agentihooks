from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING

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
