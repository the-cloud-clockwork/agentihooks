"""The swarm user services and the scheduler cron that runs `agentihooks swarm tick` every minute."""

import contextlib
import os
import shutil
import signal
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
TICK_WATCH = "agentihooks-tick-watch"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
RETIRED = (f"{UNIT}.timer", f"{UNIT}.service")
DRAIN_S = 30
KILL_WAIT_S = 10
STOP_TIMEOUT_S = 120
CGROUP_FILE = Path("/proc/self/cgroup")
CGROUP_ROOT = Path("/sys/fs/cgroup")


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
        "TimeoutStartSec=120\nKillMode=process\n"
    )
    wait_timer = (
        "[Unit]\nDescription=agentihooks swarm wait checks every fifteen seconds\n\n"
        "[Timer]\nOnBootSec=15s\nOnUnitActiveSec=15s\nAccuracySec=1s\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    watch = (
        "[Unit]\nDescription=agentihooks stale tick watcher, apart from the tick it watches\n\n"
        "[Service]\nType=simple\nEnvironment=PYTHONUNBUFFERED=1\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        f'ExecStart="{binary}" swarm tick-watch\n'
        "Restart=always\nRestartSec=5\nNotifyAccess=main\nWatchdogSec=600\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    return {
        f"{WAITS}.service": wait_service,
        f"{WAITS}.timer": wait_timer,
        f"{WAKER}.service": waker,
        f"{PROBER}.service": prober,
        f"{TICK_WATCH}.service": watch,
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


def _left(run):
    shown = run(
        ["systemctl", "--user", "show", "-p", "ControlGroup", "-p", "TasksCurrent", f"{UNIT}.service"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if shown.returncode:
        return "unknown"
    props = dict(line.partition("=")[::2] for line in shown.stdout.splitlines())
    return "0" if not props.get("ControlGroup") else props.get("TasksCurrent", "unknown")


def _drain(run):
    stop = ["systemctl", "--user", "stop", f"{UNIT}.service"]
    run(stop, capture_output=True, text=True, timeout=STOP_TIMEOUT_S, check=False)
    for name, wait_s in (("SIGTERM", DRAIN_S), ("SIGKILL", KILL_WAIT_S)):
        if _left(run) == "0":
            return ""
        kill = ["systemctl", "--user", "kill", "--kill-who=all", f"--signal={name}", f"{UNIT}.service"]
        run(kill, capture_output=True, text=True, timeout=30, check=False)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if _left(run) == "0":
                return ""
            time.sleep(1)
    left = _left(run)
    return "" if left == "0" else left


def _own_group():
    try:
        lines = CGROUP_FILE.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        _, controllers, path = line.split(":", 2)
        if path.endswith(f"/{UNIT}.service") and controllers in ("", "name=systemd"):
            return CGROUP_ROOT / ("systemd" if controllers else "") / path.lstrip("/") / "cgroup.procs"
    return None


def _others(procs):
    from scripts.swarm import tick_reaper

    pids = {int(pid) for pid in procs.read_text().split()}
    return {pid for pid in pids - tick_reaper.ancestors() if tick_reaper.alive(pid)}


def _drain_inside(procs):
    try:
        for number, wait_s in ((signal.SIGTERM, DRAIN_S), (signal.SIGKILL, KILL_WAIT_S)):
            for pid in _others(procs):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, number)
            deadline = time.monotonic() + wait_s
            while (left := _others(procs)) and time.monotonic() < deadline:
                time.sleep(1)
            if not left:
                return ""
    except OSError:
        return "unknown"
    return str(len(left))


def _retire(unit_dir, run):
    present = [unit_dir / name for name in RETIRED if (unit_dir / name).exists()]
    if not present:
        return False
    disable = ["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"]
    if run(disable, capture_output=True, text=True, timeout=30, check=False).returncode:
        print(f"the {UNIT} timer could not be disabled; its unit files stay until a later pass", file=sys.stderr)
        return False
    procs = _own_group()
    if left := _drain_inside(procs) if procs else _drain(run):
        print(
            f"the {UNIT} service group did not empty ({left} processes left); its unit files stay until a later pass",
            file=sys.stderr,
        )
        return False
    for path in present:
        path.unlink()
    return True


def supervise():
    from scripts.scheduler.service import ensure_running

    return ensure_running(_scheduler_env())


def _scheduler_env():
    from scripts.swarm.store import redis_url

    return {**os.environ, "AGENTIHOOKS_SWARM_REDIS_URL": redis_url(os.environ)}


def _off_installed(held):
    from scripts.scheduler.service import revision

    pid, root, installed = held["pid"], held.get("root"), _roots()[1]
    if not root:
        return f"the scheduler supervisor {pid} left no record of the code it loaded"
    if Path(root).resolve() != Path(installed).resolve():
        return f"the scheduler supervisor {pid} runs {root}, not the installed {installed}"
    if (loaded := held.get("revision")) != (current := revision(installed)):
        return f"the scheduler supervisor {pid} loaded revision {loaded}, not the installed revision {current}"
    return ""


def recycle():
    from scripts.scheduler import service

    env = _scheduler_env()
    held = service.holder(env)
    if held is None or not (why := _off_installed(held)):
        return
    print(f"recycling the scheduler supervisor: {why}", file=sys.stderr)
    if not service.drain(held["pid"], env):
        print(
            f"the scheduler supervisor {held['pid']} still holds its lock; the next wait pass tries again",
            file=sys.stderr,
        )


def _proven():
    from redis.exceptions import RedisError

    from scripts.scheduler import service
    from scripts.scheduler.core import Scheduler
    from scripts.swarm import scheduled_tick
    from scripts.swarm.store import redis_client

    started = (service.holder(_scheduler_env()) or {}).get("started")
    try:
        return started is not None and scheduled_tick.delivered_at(Scheduler(redis_client())) >= started
    except RedisError:
        return False


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
    kept = _kept()
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
    return schedule() and all(proc.returncode == 0 for proc in procs)


def guard(binary, unit_dir=None, run=subprocess.run):
    unit_dir = unit_dir or UNIT_DIR
    recycle()
    supervise()
    if not _kept():
        if any((unit_dir / name).exists() for name in RETIRED) and _left(run) == "0" and _proven():
            return ensure(binary, unit_dir, run)
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
