"""Start the lost runner watchdog in cluster mode for the swarm's repository at most once a minute.

CI runners cannot read the cluster, so the tick host reads runner pods and nodes for it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.swarm.store import PREFIX

if TYPE_CHECKING:
    from scripts.swarm.store import RedisStore, SwarmConfig

WORKFLOW = Path(".github/workflows/test.yml")
INTERVAL_MS = 60 * 1000


def home() -> Path:
    return Path(os.environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks")


def key(repo: str) -> str:
    return ":".join((PREFIX, "lost-runner", repo))


def command(lock: Path) -> list[str]:
    args = ["-I", "-m", "scripts.ci_lost_runner", "--repo", "{owner}/{repo}", "--cluster", "--lock", str(lock)]
    return [sys.executable, *args]


def sweep(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    now_ms: int,
    popen: Callable = subprocess.Popen,
    which: Callable = shutil.which,
) -> list[str]:
    ready = config.repo and (Path(config.repo) / WORKFLOW).is_file() and home().is_dir() and which("kubectl")
    if not ready:
        return []
    with open(home() / "lost-runner.log", "a") as log:
        if store.redis.set(key(config.repo), now_ms, nx=True, px=INTERVAL_MS):
            popen(command(home() / "lost-runner.lock"), cwd=config.repo, stdout=log, stderr=log, start_new_session=True)
    return []
