"""Master fallback tick: a small loop on the master hive, waiting a minute on the agentihooks scheduler between passes,
that starts a new master for a swarm that has had no live master for AGENTIHOOKS_MASTER_FALLBACK_MINUTES (default 20),
whether or not the main tick runs. It reads only the swarm's master records and the local live agent names, and acts
only through agentihooks swarm master up."""

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
PASS_TIMEOUT_S = 50
SYSTEMCTL_TIMEOUT_S = 60
MODULE = "scripts.swarm.master_fallback"
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
    """Start a new master the same way the operator does from a terminal, in its own session so a timed out pass
    never cuts the launch short; returns the launch process id."""
    command = ["agentihooks", "swarm", slug, "master", "up", "--new"]
    return subprocess.Popen(command, start_new_session=True).pid


def check(
    store: Any,
    environ: Mapping[str, str],
    live_names: Callable[[], set[str]],
    now_ms: int,
    down_since: dict[str, int],
    launch: Callable[[str], int] = start,
) -> list[str]:
    """One fallback pass over every watched swarm; returns one line per master it started."""
    limit_ms, live = int(minutes(environ) * 60 * 1000), live_names()
    lines: list[str] = []
    for slug in swarms(environ):
        if store.config(slug).state in QUIET:
            down_since.pop(slug, None)
            continue
        if step(slug, master_live(store, slug, live), now_ms, down_since, limit_ms):
            code = launch(slug)
            lines.append(json.dumps({"swarm": slug, "master_up": code, "at": now_ms}))
    return lines


def run(environ: Mapping[str, str]) -> None:
    """Run one fallback pass a minute, each in its own process with a hard timeout, so a hung herdr or Redis call
    can never stop the loop; the wait between passes is bounded the same way."""
    from scripts.scheduler import process_timer

    while True:
        try:
            subprocess.run([sys.executable, "-m", MODULE, "pass"], check=False, timeout=PASS_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            print(json.dumps({"error": f"pass timed out after {PASS_TIMEOUT_S}s"}), flush=True)
        process_timer.wait(INTERVAL_S, timeout=2 * INTERVAL_S)


def pass_once(environ: Mapping[str, str], state: Path = STATE) -> None:
    """One fallback pass: start a master where the window ran out and save the outage starts."""
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import connect  # pyright: ignore[reportUnknownVariableType]

    down_since = load(state)
    try:
        runtime: Any = HerdrRuntime()
        store: Any = connect()
        for line in check(store, environ, runtime.live_names, int(time.time() * 1000), down_since, start):
            print(line, flush=True)
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(down_since))
    except Exception as error:
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}), flush=True)


def load(state: Path) -> dict[str, int]:
    """The outage start per swarm the last pass saw, empty when no pass has saved one."""
    return json.loads(state.read_text()) if state.exists() else {}


def unit_text(python: str, repo: str, environ: Mapping[str, str]) -> str:
    """The systemd user service that keeps the fallback loop running on the master hive."""
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
    """Write and start the fallback service for the current user, retiring the timer an earlier install wrote."""
    repo = str(Path(__file__).resolve().parents[2])
    folder = Path.home() / ".config" / "systemd" / "user"
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"], check=False, timeout=SYSTEMCTL_TIMEOUT_S
    )
    (folder / f"{UNIT}.timer").unlink(missing_ok=True)
    path = folder / f"{UNIT}.service"
    path.write_text(unit_text(sys.executable, repo, environ))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=SYSTEMCTL_TIMEOUT_S)
    subprocess.run(["systemctl", "--user", "enable", f"{UNIT}.service"], check=True, timeout=SYSTEMCTL_TIMEOUT_S)
    subprocess.run(["systemctl", "--user", "restart", f"{UNIT}.service"], check=True, timeout=SYSTEMCTL_TIMEOUT_S)
    return path


def main(argv: list[str]) -> None:
    """run keeps the loop going, pass makes one pass, install writes and starts the service."""
    if argv == ["install"]:
        print(install(os.environ))
        return
    if argv == ["pass"]:
        pass_once(os.environ)
        return
    run(os.environ)


if __name__ == "__main__":
    main(sys.argv[1:])
