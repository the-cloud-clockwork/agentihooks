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
    script = ["-I", "-m", "scripts.ci_lost_runner", "--repo", "{owner}/{repo}", "--cluster", "--lock", str(lock)]
    return [sys.executable, *script]


def sweep(
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    now_ms: int,
    popen: Callable = subprocess.Popen,
    which: Callable = shutil.which,
) -> list[str]:
    if not config.repo or not (Path(config.repo) / WORKFLOW).is_file() or not which("kubectl"):
        return []
    if not store.redis.set(key(config.repo), now_ms, nx=True, px=INTERVAL_MS):
        return []
    with open(home() / "lost-runner.log", "a") as log:
        popen(command(home() / "lost-runner.lock"), cwd=config.repo, stdout=log, stderr=log, start_new_session=True)
    return []
