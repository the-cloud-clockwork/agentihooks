import json
import os
import pwd
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis import Redis

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
    from scripts.swarm.store import redis_client

    return Scheduler(redis_client())


def scheduled(scheduler) -> list[str]:
    entries = (json.loads(raw) for raw in scheduler.redis.hvals(scheduler.key("entries")))
    return [entry["id"] for entry in entries if entry["owner"] == UNIT and entry["state"] in LIVE]


def install_timer(python: str, root: str) -> str:
    from hooks.lifecycle.maintenance import setup

    return "\n  ".join([*_remove_units(), _schedule(python, root), *setup()])


def _schedule(python: str, root: str) -> str:
    from redis.exceptions import RedisError

    from scripts.scheduler.service import ensure_running

    try:
        scheduler = _scheduler()
        for entry_id in scheduled(scheduler):
            scheduler.get(entry_id).cancel()
        cron = scheduler.cron(owner=UNIT, payload=json.dumps({"python": python, "root": root}), expression=CRON)
        cron.timeout.connect(sweep)
        cron.start()
    except RedisError:
        return "[--] Redis unreachable: lifecycle sweep not scheduled, sessions trigger the sweep instead"
    ensure_running()
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
    from redis.exceptions import RedisError

    messages = _remove_units()
    try:
        scheduler = _scheduler()
        entries = scheduled(scheduler)
        for entry_id in entries:
            scheduler.get(entry_id).cancel()
    except RedisError:
        return "\n  ".join([*messages, "[--] Redis unreachable: lifecycle sweep schedule not checked"])
    if entries:
        messages.append("[OK] Lifecycle sweep removed from the agentihooks scheduler")
    return "\n  ".join(messages or [f"[--] {UNIT} not scheduled"])


def sweep(event: dict, delivery_id: str, redis: "Redis") -> None:
    from hooks._redis import redis_key

    if not redis.set(redis_key("scheduler", f"{UNIT}:{delivery_id}"), "1", nx=True, ex=86400):
        return
    target = json.loads(event["payload"])
    subprocess.Popen(
        ["nice", "-n", "10", target["python"], "-m", "scripts.gc_cli", "gc", "--enforce"],
        cwd=target["root"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
