"""The systemd user timer that runs `agentihooks swarm tick` every minute; nothing runs between ticks."""

import subprocess
from pathlib import Path

UNIT = "agentihooks-swarm"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


def units(binary):
    service = (
        "[Unit]\nDescription=agentihooks swarm tick\n\n"
        f"[Service]\nType=oneshot\nExecStart={binary} swarm tick\nTimeoutStartSec=900\n"
    )
    timer = (
        "[Unit]\nDescription=agentihooks swarm tick every minute\n\n"
        "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=60s\nAccuracySec=5s\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    return {f"{UNIT}.service": service, f"{UNIT}.timer": timer}


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
    proc = run(["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"], capture_output=True, text=True, timeout=30)
    return proc.returncode == 0
