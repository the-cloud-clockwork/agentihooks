"""Master fallback tick: a small loop on the master hive that starts a new master for a swarm that has had no live
master for AGENTIHOOKS_MASTER_FALLBACK_MINUTES (default 20), whether or not the main tick runs. It reads only the
swarm's master records and the local live agent names, and acts only through agentihooks swarm <slug> master up."""

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

MINUTES = "AGENTIHOOKS_MASTER_FALLBACK_MINUTES"
SWARMS = "AGENTIHOOKS_MASTER_FALLBACK_SWARMS"
DEFAULT_MINUTES = 20.0
INTERVAL_S = 60
UNIT = "agentihooks-master-fallback.service"
QUIET = ("stopped", "stopping")


def minutes(environ: Mapping[str, str]) -> float:
    """The minutes a swarm may go without a live master before the fallback starts one."""
    return float(environ.get(MINUTES) or DEFAULT_MINUTES)


def swarms(environ: Mapping[str, str]) -> list[str]:
    """The swarm slugs this fallback watches."""
    return [slug.strip() for slug in environ.get(SWARMS, "").split(",") if slug.strip()]


def master_live(store: Any, slug: str, live: set[str]) -> bool:
    """Whether any master of the swarm is a live agent on this host."""
    return any(agent.lane == "master" and agent.name in live for agent in store.agents(slug))


def step(slug: str, live: bool, now_ms: int, down_since: dict[str, int], limit_ms: int) -> bool:
    """Record the outage start and say whether the fallback must start a master now; a start restarts the wait."""
    if live:
        down_since.pop(slug, None)
        return False
    since = down_since.setdefault(slug, now_ms)
    if now_ms - since < limit_ms:
        return False
    down_since[slug] = now_ms
    return True


def start(slug: str) -> int:
    """Start a new master the same way the operator does from a terminal."""
    command = ["agentihooks", "swarm", slug, "master", "up", "--new"]
    return subprocess.run(command, check=False).returncode


def check(
    store: Any,
    environ: Mapping[str, str],
    live_names: Callable[[], set[str]],
    now_ms: int,
    down_since: dict[str, int],
    launch: Callable[[str], int] = start,
) -> list[str]:
    """One fallback pass over every watched swarm; returns one line per master it started."""
    limit_ms, live, lines = int(minutes(environ) * 60 * 1000), live_names(), []
    for slug in swarms(environ):
        if store.config(slug).state in QUIET:
            down_since.pop(slug, None)
            continue
        if step(slug, master_live(store, slug, live), now_ms, down_since, limit_ms):
            code = launch(slug)
            lines.append(json.dumps({"swarm": slug, "master_up": code, "at": now_ms}))
    return lines


def run(environ: Mapping[str, str]) -> None:
    """Run the fallback pass every minute until stopped; a failed pass is logged and the loop goes on."""
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import connect

    down_since: dict[str, int] = {}
    runtime: Any = HerdrRuntime()
    while True:
        try:
            store: Any = connect()
            for line in check(store, environ, runtime.live_names, int(time.time() * 1000), down_since):
                print(line, flush=True)
        except Exception as error:
            print(json.dumps({"error": f"{type(error).__name__}: {error}"}), flush=True)
        time.sleep(INTERVAL_S)


def unit_text(python: str, repo: str, environ: Mapping[str, str]) -> str:
    """The systemd user unit that keeps the fallback running on the master hive."""
    return (
        "[Unit]\nDescription=agentihooks master fallback tick\n\n"
        "[Service]\n"
        f"WorkingDirectory={repo}\n"
        f"Environment={SWARMS}={environ.get(SWARMS, '')}\n"
        f"Environment={MINUTES}={minutes(environ):g}\n"
        f'ExecStart="{python}" -m scripts.swarm.master_fallback run\n'
        "Restart=always\nRestartSec=10\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def install(environ: Mapping[str, str]) -> Path:
    """Write and start the fallback unit for the current user."""
    repo = str(Path(__file__).resolve().parents[2])
    path = Path.home() / ".config" / "systemd" / "user" / UNIT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(sys.executable, repo, environ), encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", UNIT], check=True)
    subprocess.run(["systemctl", "--user", "restart", UNIT], check=True)
    return path


def main(argv: list[str]) -> None:
    """run keeps the loop going; install writes and starts its unit."""
    if argv[:1] == ["install"]:
        print(install(os.environ))
        return
    run(os.environ)


if __name__ == "__main__":
    main(sys.argv[1:])
