"""The swarm user services and the scheduler cron that runs `agentihooks swarm tick` every minute."""

import os
import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path

UNIT = "agentihooks-swarm"
WAKER = "agentihooks-inbox-waker"
DROPPED = ("agentihooks-alert-intake.service",)
PROBER = "agentihooks-quota-prober"
WAITS = "agentihooks-swarm-waits"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
RETIRED = (f"{UNIT}.timer", f"{UNIT}.service")


def units(binary):
    waker = (
        "[Unit]\nDescription=agentihooks inbox waker for Codex panes\n\n"
        "[Service]\nType=simple\nEnvironment=PYTHONUNBUFFERED=1\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        f'ExecStart="{binary}" swarm waker\n'
        "Restart=always\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    prober = (
        "[Unit]\nDescription=agentihooks quota prober for the ledger page refresh\n\n"
        "[Service]\nType=simple\nEnvironment=PYTHONUNBUFFERED=1\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        f'ExecStart="{binary}" swarm prober\n'
        "Restart=always\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    wait_service = (
        "[Unit]\nDescription=agentihooks swarm wait checks\n\n"
        "[Service]\nType=oneshot\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        f'ExecStart="{binary}" swarm waits\n'
        "TimeoutStartSec=120\n"
    )
    wait_timer = (
        "[Unit]\nDescription=agentihooks swarm wait checks every fifteen seconds\n\n"
        "[Timer]\nOnBootSec=15s\nOnUnitActiveSec=15s\nAccuracySec=1s\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return {
        f"{WAITS}.service": wait_service,
        f"{WAITS}.timer": wait_timer,
        f"{WAKER}.service": waker,
        f"{PROBER}.service": prober,
    }


def _current(path, text):
    return path.is_file() and path.read_text() == text


def tick_units(binary):
    service = (
        "[Unit]\nDescription=agentihooks swarm tick\n\n"
        "[Service]\nType=oneshot\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        f'ExecStart="{binary}" swarm tick\n'
        "TimeoutStartSec=540\nKillMode=process\n"
    )
    timer = (
        "[Unit]\nDescription=agentihooks swarm tick every minute\n\n"
        "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=60s\nAccuracySec=5s\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return {f"{UNIT}.service": service, f"{UNIT}.timer": timer}


def _services(names):
    return [name for name in names if name.endswith(".service") and name not in (f"{UNIT}.service", f"{WAITS}.service")]


def _retire(unit_dir, run):
    present = [unit_dir / name for name in RETIRED if (unit_dir / name).exists()]
    if not present:
        return False
    disable = ["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"]
    if run(disable, capture_output=True, text=True, timeout=30, check=False).returncode:
        print(f"the {UNIT} timer could not be disabled; its unit files stay until a later pass", file=sys.stderr)
        return False
    for path in present:
        path.unlink()
    return True


def supervise():
    from scripts.scheduler.service import ensure_running
    from scripts.swarm.store import redis_url

    return ensure_running({**os.environ, "AGENTIHOOKS_SWARM_REDIS_URL": redis_url(os.environ)})


def schedule():
    from redis.exceptions import RedisError

    from scripts.scheduler.core import Scheduler
    from scripts.swarm import scheduled_tick
    from scripts.swarm.store import redis_client

    try:
        scheduled_tick.ensure(Scheduler(redis_client()))
    except RedisError as exc:
        print(f"the scheduled swarm tick could not be registered: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    supervise()
    return True


def undelivered():
    from scripts.scheduler.core import Scheduler
    from scripts.swarm import scheduled_tick
    from scripts.swarm.store import redis_client

    return scheduled_tick.undelivered(Scheduler(redis_client()), _roots()[1], time.time())


def _kept():
    from redis.exceptions import RedisError

    try:
        return undelivered()
    except RedisError as exc:
        return f"the swarm tick delivery could not be read: {type(exc).__name__}: {exc}"


def _worktree_run(unit_dir):
    from scripts.targets._common import _install_module

    running = _install_module().AGENTIHOOKS_ROOT.resolve()
    if unit_dir.resolve() != UNIT_DIR.resolve() or not (running / ".git").is_file():
        return ""
    return (
        f"this run comes from the git worktree {running}, so it leaves the shared swarm timer units in {unit_dir} alone"
    )


def _foreign_run(unit_dir):
    from scripts.targets._common import _install_module

    if refusal := _worktree_run(unit_dir):
        return refusal
    _i = _install_module()
    running, installed = _i.AGENTIHOOKS_ROOT.resolve(), _i.install_root().resolve()
    if running == installed or unit_dir.resolve() != UNIT_DIR.resolve():
        return ""
    return (
        f"this run comes from {running}, not the installed agentihooks at {installed}, "
        f"so it leaves the shared swarm timer units in {unit_dir} alone"
    )


def ensure(binary, unit_dir=None, run=subprocess.run):
    unit_dir = unit_dir or UNIT_DIR
    refusal = _foreign_run(unit_dir)
    if refusal:
        print(refusal, file=sys.stderr)
        return False
    unit_dir.mkdir(parents=True, exist_ok=True)
    scheduled = schedule()
    kept = _kept() if scheduled else "the scheduled swarm tick is not registered"
    expected = units(binary)
    if kept:
        print(f"the {UNIT} timer stays: {kept}", file=sys.stderr)
        changed, expected = False, {**expected, **tick_units(binary)}
    else:
        changed = _retire(unit_dir, run)
    for name, text in expected.items():
        path = unit_dir / name
        if not _current(path, text):
            path.write_text(text)
            changed = True
    for name in DROPPED:
        if (unit_dir / name).exists():
            run(
                ["systemctl", "--user", "disable", "--now", name],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            (unit_dir / name).unlink()
            changed = True
    if changed:
        run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=30, check=False)
    procs = [
        run(["systemctl", "--user", "enable", "--now", name], capture_output=True, text=True, timeout=30)
        for name in [*_services(expected), f"{WAITS}.timer", *([f"{UNIT}.timer"] if kept else [])]
    ]
    return scheduled and all(proc.returncode == 0 for proc in procs)


def guard(binary, unit_dir=None, run=subprocess.run):
    unit_dir = unit_dir or UNIT_DIR
    supervise()
    if not _kept():
        return True
    enabled = ["systemctl", "--user", "is-enabled", "--quiet", f"{UNIT}.timer"]
    if all(_current(unit_dir / name, text) for name, text in tick_units(binary).items()) and not (
        run(enabled, capture_output=True, text=True, timeout=30).returncode
    ):
        return True
    return ensure(binary, unit_dir, run)


def reconcile(binary, unit_dir=None, run=subprocess.run):
    unit_dir = unit_dir or UNIT_DIR
    if refusal := _worktree_run(unit_dir):
        print(refusal, file=sys.stderr)
        return False
    expected = units(binary)
    current = all(_current(unit_dir / name, text) for name, text in expected.items())
    if current and not any((unit_dir / name).exists() for name in (*RETIRED, *DROPPED)):
        names = [*_services(expected), f"{WAITS}.timer"]
        enabled = ["systemctl", "--user", "is-enabled", "--quiet"]
        if all(run([*enabled, name], capture_output=True, text=True, timeout=30).returncode == 0 for name in names):
            return schedule()
    return ensure(binary, unit_dir, run)


def _roots():
    from scripts.targets._common import _install_module

    _i = _install_module()
    return _i.AGENTIHOOKS_ROOT, _i.install_root()


def installed_refusal():
    running, installed = (Path(root).resolve() for root in _roots())
    if running == installed:
        return ""
    return f"this run comes from {running}, not the installed agentihooks at {installed}"


def entry_point(scripts_dir=None, which=shutil.which):
    script = Path(scripts_dir or sysconfig.get_path("scripts")) / "agentihooks"
    found = which("agentihooks")
    if found and Path(found).resolve() == script.resolve():
        return found
    return str(script)
