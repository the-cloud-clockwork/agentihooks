"""Master fallback tick: a one-minute systemd timer on the master hive that starts a new master for a swarm that has
had no live master for AGENTIHOOKS_MASTER_FALLBACK_MINUTES (default 20), whether or not the main tick runs. It reads
only the swarm's master records and the local live agent names, and acts only through agentihooks swarm master up."""

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
UNIT = "agentihooks-master-fallback"
STATE = Path.home() / ".agentihooks" / "master-fallback.json"
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


def run(environ: Mapping[str, str], state: Path = STATE) -> None:
    """One fallback pass, started every minute by its systemd timer; the outage starts survive in the state file."""
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import connect

    down_since = load(state)
    runtime: Any = HerdrRuntime()
    store: Any = connect()
    for line in check(store, environ, runtime.live_names, int(time.time() * 1000), down_since):
        print(line, flush=True)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(down_since), encoding="utf-8")


def load(state: Path) -> dict[str, int]:
    """The outage start per swarm the last pass saw, empty when no pass has saved one."""
    return json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}


def unit_text(python: str, repo: str, environ: Mapping[str, str]) -> dict[str, str]:
    """The systemd user service that runs one fallback pass and the timer that starts it every minute."""
    service = (
        "[Unit]\nDescription=agentihooks master fallback tick\n\n"
        "[Service]\nType=oneshot\n"
        f"WorkingDirectory={repo}\n"
        f"Environment={SWARMS}={environ.get(SWARMS, '')}\n"
        f"Environment={MINUTES}={minutes(environ):g}\n"
        f'ExecStart="{python}" -m scripts.swarm.master_fallback run\n'
    )
    timer = (
        "[Unit]\nDescription=agentihooks master fallback tick every minute\n\n"
        f"[Timer]\nOnBootSec={INTERVAL_S}\nOnUnitActiveSec={INTERVAL_S}\nAccuracySec=5\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return {f"{UNIT}.service": service, f"{UNIT}.timer": timer}


def install(environ: Mapping[str, str]) -> Path:
    """Write the fallback service and timer for the current user and start the timer."""
    repo = str(Path(__file__).resolve().parents[2])
    folder = Path.home() / ".config" / "systemd" / "user"
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(["systemctl", "--user", "disable", "--now", f"{UNIT}.service"], check=False)
    for name, text in unit_text(sys.executable, repo, environ).items():
        (folder / name).write_text(text, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"], check=True)
    return folder / f"{UNIT}.timer"


def main(argv: list[str]) -> None:
    """run makes one pass; install writes the service and timer and starts the timer."""
    if argv[:1] == ["install"]:
        print(install(os.environ))
        return
    run(os.environ)


if __name__ == "__main__":
    main(sys.argv[1:])
