"""The systemd user timer that runs `agentihooks swarm tick` every minute; nothing runs between ticks."""

import shutil
import subprocess
import sysconfig
from pathlib import Path

from scripts.targets._common import _install_module

UNIT = "agentihooks-swarm"
WAKER = "agentihooks-inbox-waker"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


def units(binary):
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
    waker = (
        "[Unit]\nDescription=agentihooks inbox waker for Codex panes\n\n"
        "[Service]\nType=simple\nEnvironment=PYTHONUNBUFFERED=1\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        f'ExecStart="{binary}" swarm waker\n'
        "Restart=always\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    return {f"{UNIT}.service": service, f"{UNIT}.timer": timer, f"{WAKER}.service": waker}


def ensure(binary, unit_dir=UNIT_DIR, run=subprocess.run):
    unit_dir.mkdir(parents=True, exist_ok=True)
    changed = False
    for name, text in units(binary).items():
        path = unit_dir / name
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
            changed = True
    if changed:
        run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=30, check=False)
    run(["systemctl", "--user", "enable", "--now", f"{WAKER}.service"], capture_output=True, text=True, timeout=30)
    proc = run(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"], capture_output=True, text=True, timeout=30)
    return proc.returncode == 0


def _roots():
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


def ensure_installed(unit_dir=UNIT_DIR, run=subprocess.run):
    if why := installed_refusal():
        return f"{why}, so the shared swarm timer was left as it is"
    if not ensure(entry_point(), unit_dir, run):
        return "the systemd timer could not be enabled; run agentihooks swarm tick yourself"
    return ""
