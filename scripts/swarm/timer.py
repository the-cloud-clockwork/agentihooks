"""The swarm user services and the scheduler cron that runs `agentihooks swarm tick` every minute."""

import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

UNIT = "agentihooks-swarm"
WAKER = "agentihooks-inbox-waker"
ALERTS = "agentihooks-alert-intake"
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
    alerts = (
        "[Unit]\nDescription=agentihooks swarm alert intake for Grafana\n\n"
        "[Service]\nType=simple\nEnvironment=PYTHONUNBUFFERED=1\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        f'ExecStart="{binary}" swarm alerts\n'
        "Restart=on-failure\nRestartSec=5\n\n"
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
        f"{ALERTS}.service": alerts,
        f"{PROBER}.service": prober,
    }


def _current(path, text):
    return path.is_file() and path.read_text() == text


def _services(names):
    return [name for name in names if name.endswith(".service") and name != f"{WAITS}.service"]


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


def schedule():
    from redis.exceptions import RedisError

    from scripts.scheduler.core import Scheduler
    from scripts.scheduler.service import ensure_running
    from scripts.swarm import scheduled_tick
    from scripts.swarm.store import redis_client, redis_url

    try:
        scheduled_tick.ensure(Scheduler(redis_client()))
    except RedisError as exc:
        print(f"the scheduled swarm tick could not be registered: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    ensure_running({**os.environ, "AGENTIHOOKS_SWARM_REDIS_URL": redis_url(os.environ)})
    return True


def _foreign_run(unit_dir):
    from scripts.targets._common import _install_module

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
    changed, expected = _retire(unit_dir, run), units(binary)
    for name, text in expected.items():
        path = unit_dir / name
        if not _current(path, text):
            path.write_text(text)
            changed = True
    if changed:
        run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=30, check=False)
    procs = [
        run(["systemctl", "--user", "enable", "--now", name], capture_output=True, text=True, timeout=30)
        for name in [*_services(expected), f"{WAITS}.timer"]
    ]
    scheduled = schedule()
    return scheduled and all(proc.returncode == 0 for proc in procs)


def reconcile(binary, unit_dir=None, run=subprocess.run):
    unit_dir = unit_dir or UNIT_DIR
    expected = units(binary)
    current = all(_current(unit_dir / name, text) for name, text in expected.items())
    if current and not any((unit_dir / name).exists() for name in RETIRED):
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
