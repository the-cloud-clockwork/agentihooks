import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis import Redis

REPO = Path(__file__).resolve().parent
LINKS = ("guard-daemon.sh", "guard-config.json")
LEGACY = ("guard-daemon.timer", "guard-daemon.service")
OWNER = "agentihooks-host-guard"
EVERY = 60
TICK = "scripts.host_guard.sync:tick"
LIVE = ("ready", "active", "pending")


def _unit_dir(home: Path) -> Path:
    return home / ".config" / "systemd" / "user"


def _account_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _live(home: Path) -> bool:
    return home.resolve() == _account_home().resolve()


def _systemctl(*args: str) -> bool:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _scheduler():
    from scripts.scheduler import Scheduler
    from scripts.swarm.store import redis_client

    return Scheduler(redis_client())


def _entries(scheduler) -> list:
    return [entry for entry in scheduler.owned(OWNER) if entry.state in LIVE]


def _records() -> list[dict] | None:
    from redis.exceptions import RedisError

    try:
        return [entry.record for entry in _entries(_scheduler())]
    except RedisError:
        return None


def _for(home: Path, records: list[dict]) -> list[dict]:
    return [record for record in records if json.loads(record["payload"])["script"] == str(home / LINKS[0])]


def _current(record: dict) -> bool:
    return record["delay"] == EVERY and TICK in [slot.get("reference") for slot in record["slots"]]


def _start() -> None:
    from scripts.scheduler.service import ensure_running
    from scripts.swarm.store import redis_url

    ensure_running({**os.environ, "AGENTIHOOKS_SWARM_REDIS_URL": redis_url(os.environ)})


def drift(home: Path) -> list[str]:
    found = []
    for name in LINKS:
        path = home / name
        if not path.is_symlink() and not path.exists():
            found.append(f"{path} is missing")
        elif not path.is_symlink() or path.resolve() != (REPO / name).resolve():
            found.append(f"{path} is not a link to {REPO / name}")
    for name in LEGACY:
        path = _unit_dir(home) / name
        if path.exists():
            found.append(f"{path} still runs the guard on systemd instead of the agentihooks scheduler")
    records = _records()
    if records is None:
        found.append("the agentihooks scheduler is unreachable, so the guard schedule was not checked")
    elif not any(_current(record) for record in _for(home, records)):
        found.append(f"{home / LINKS[0]} is not scheduled every {EVERY}s on the agentihooks scheduler")
    return found


def _remove_units(home: Path) -> bool:
    units = [_unit_dir(home) / name for name in LEGACY]
    if not any(unit.exists() for unit in units):
        return False
    live = _live(home)
    running = live and _systemctl("is-enabled", "--quiet", LEGACY[0])
    if live:
        _systemctl("disable", "--now", LEGACY[0])
    for unit in units:
        unit.unlink(missing_ok=True)
    if live:
        _systemctl("daemon-reload")
    return running


def _schedule(home: Path) -> str:
    from redis.exceptions import RedisError

    try:
        scheduler = _scheduler()
        payload = json.dumps({"script": str(home / LINKS[0])})
        stale = [entry for entry in _entries(scheduler) if entry.record["payload"] == payload]
        entry = scheduler.interval(owner=OWNER, payload=payload, every=EVERY)
        entry.timeout.connect(tick)
        entry.start()
        for old in stale:
            old.cancel()
    except RedisError:
        return f"[!!] Host guard linked in {home}; Redis is unreachable, so the guard is not scheduled"
    _start()
    return f"[OK] Host guard installed in {home} from {REPO}, every {EVERY}s on the agentihooks scheduler"


def install(home: Path, enable: bool) -> str:
    for name in LINKS:
        staged = home / f".{name}.agentihooks"
        staged.unlink(missing_ok=True)
        staged.symlink_to(REPO / name)
        staged.replace(home / name)
    if _remove_units(home) or enable:
        return _schedule(home)
    return f"[--] Host guard linked in {home} from {REPO}; agentihooks host-guard install schedules it"


def reconcile(home: Path) -> str:
    paths = [home / name for name in LINKS] + [_unit_dir(home) / name for name in LEGACY]
    if not any(path.is_symlink() or path.exists() for path in paths):
        return "[--] Host guard not installed; agentihooks host-guard install adds it"
    if not drift(home):
        _start()
        return f"[OK] Host guard in sync with {REPO}"
    records = _records()
    if records is None:
        return "[!!] Host guard not reconciled: the agentihooks scheduler is unreachable"
    return install(home, enable=bool(_for(home, records)))


def tick(event: dict, delivery_id: str, redis: "Redis") -> None:
    from hooks._redis import redis_key

    if not redis.set(redis_key("scheduler", f"{OWNER}:{delivery_id}"), "1", nx=True, ex=3600):
        return
    script = json.loads(event["payload"])["script"]
    limit = ["timeout", str(EVERY * 2)] if shutil.which("timeout") else []
    try:
        subprocess.Popen(
            [*limit, script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        from hooks.common import log

        log("host guard failed to start", {"error": str(error)})


def run(action: str, home: Path) -> int:
    if action == "install":
        message = install(home, enable=True)
        print(message)
        return 0 if message.startswith("[OK]") else 1
    found = drift(home)
    print("\n".join(found) if found else f"[OK] Host guard in sync with {REPO}")
    return 1 if found else 0
