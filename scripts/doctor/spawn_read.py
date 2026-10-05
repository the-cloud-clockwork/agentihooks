from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore


def records(store: RedisStore, slug: str, since: str = "1 hour ago", run: Callable = subprocess.run) -> dict:
    from scripts.swarm.store import codex_split

    journal = run(
        ["journalctl", "--user", "-u", "agentihooks-swarm.service", "--since", since, "-o", "cat", "--no-pager"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    target, _ = codex_split(store.config(slug), os.environ)
    return {
        "slug": slug,
        "target": target,
        "spawns": store.spawns(slug),
        "agents": [asdict(a) for a in store.agents(slug)],
        "restored": store.restored(slug),
        "actions": [
            line for line in journal.stdout.splitlines() if line.startswith(f"{slug}: ") and "spawn failed" in line
        ],
    }
