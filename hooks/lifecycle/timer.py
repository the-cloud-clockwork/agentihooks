import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.events.bus import StateStore

UNIT = "agentihooks-gc"
CRON = "0 * * * *"
LIVE = ("ready", "active", "pending")


def unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _live_home() -> bool:
    return Path.home().resolve() == Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _systemctl(*args: str) -> bool:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _scheduler():
    from scripts.scheduler import Scheduler

    return Scheduler()


def scheduled(scheduler) -> list[str]:
    return [entry["id"] for entry in scheduler.records() if entry["owner"] == UNIT and entry["state"] in LIVE]


def install_timer(python: str, root: str) -> str:
    from hooks.lifecycle.maintenance import setup

    return "\n  ".join([*_remove_units(), _schedule(python, root), *setup()])


def _schedule(python: str, root: str) -> str:
    from hooks.config import AGENTIHOOKS_HOME
    from scripts.events import state
    from scripts.scheduler.service import ensure_running
    from scripts.swarm.store import redis_url

    try:
        scheduler = _scheduler()
        for entry_id in scheduled(scheduler):
            scheduler.get(entry_id).cancel()
        payload = json.dumps({"python": python, "root": root, "home": str(AGENTIHOOKS_HOME)})
        cron = scheduler.cron(owner=UNIT, payload=payload, expression=CRON)
        cron.timeout.connect(sweep)
        cron.start()
    except state.unavailable():
        return "[--] Redis unreachable: lifecycle sweep not scheduled, sessions trigger the sweep instead"
    ensure_running({**os.environ, "AGENTIHOOKS_SWARM_REDIS_URL": redis_url(os.environ)})
    return f"[OK] Lifecycle sweep scheduled hourly on the agentihooks scheduler ({CRON})"


def _remove_units() -> list[str]:
    units = [unit_dir() / f"{UNIT}.{suffix}" for suffix in ("timer", "service")]
    if not any(unit.exists() for unit in units):
        return []
    if _live_home():
        _systemctl("disable", "--now", f"{UNIT}.timer")
    for unit in units:
        unit.unlink(missing_ok=True)
    if _live_home():
        _systemctl("daemon-reload")
    return [f"[OK] Removed the systemd {UNIT}.timer"]


def remove_timer() -> str:
    from scripts.events import state

    messages = _remove_units()
    try:
        scheduler = _scheduler()
        entries = scheduled(scheduler)
        for entry_id in entries:
            scheduler.get(entry_id).cancel()
    except state.unavailable():
        return "\n  ".join([*messages, "[--] Redis unreachable: lifecycle sweep schedule not checked"])
    if entries:
        messages.append("[OK] Lifecycle sweep removed from the agentihooks scheduler")
    return "\n  ".join(messages or [f"[--] {UNIT} not scheduled"])


def reconcile_units(python: str, root: str, enabled: bool) -> str:
    from scripts.host_guard.sync import reconcile

    schedule = install_timer(python, root) if enabled else remove_timer()
    return f"{schedule}\n  {reconcile(Path.home())}"


def sweep(event: dict, delivery_id: str, state: "StateStore") -> None:
    from scripts.scheduler.slots import first_delivery

    if not first_delivery(state, UNIT, delivery_id):
        return
    target = json.loads(event["payload"])
    limit = ["timeout", "3600"] if shutil.which("timeout") else []
    idle = ["ionice", "-c", "3"] if shutil.which("ionice") else []
    try:
        subprocess.Popen(
            [*limit, "nice", "-n", "10", *idle, target["python"], "-m", "scripts.gc_cli", "gc", "--enforce"],
            cwd=target["root"],
            env={**os.environ, "AGENTIHOOKS_HOME": target["home"]},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        from hooks.common import log

        log("lifecycle sweep failed to start", {"error": str(error)})
