import os
import pwd
import subprocess
from pathlib import Path

UNIT = "agentihooks-gc"

SERVICE = """[Unit]
Description=agentihooks workspace lifecycle sweep

[Service]
Type=oneshot
WorkingDirectory={root}
ExecStart={python} -m scripts.gc_cli gc --enforce
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=3600
{environment}"""

TIMER = """[Unit]
Description=Hourly agentihooks workspace lifecycle sweep

[Timer]
OnBootSec=2h
OnUnitActiveSec=1h

[Install]
WantedBy=timers.target
"""


def unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _live_home() -> bool:
    return Path.home().resolve() == Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _systemctl(*args: str) -> bool:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def render(python: str, root: str) -> tuple[str, str]:
    home = os.environ.get("AGENTIHOOKS_HOME", "")
    environment = f"Environment=AGENTIHOOKS_HOME={home}\n" if home else ""
    return SERVICE.format(python=python, root=root, environment=environment), TIMER


def installed() -> bool:
    return (unit_dir() / f"{UNIT}.timer").exists()


def install_timer(python: str, root: str) -> str:
    service, timer = render(python, root)
    folder = unit_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{UNIT}.service").write_text(service, encoding="utf-8")
    (folder / f"{UNIT}.timer").write_text(timer, encoding="utf-8")
    if not _live_home():
        return f"[OK] Wrote {UNIT}.timer (not enabled: HOME is not this user's home)"
    if _systemctl("daemon-reload") and _systemctl("enable", "--now", f"{UNIT}.timer"):
        return f"[OK] Lifecycle sweep {UNIT}.timer enabled (hourly, first run 2 h after boot)"
    return f"[--] Wrote {UNIT}.timer; no systemd user session, sessions trigger the sweep instead"


def remove_timer() -> str:
    if not installed():
        return f"[--] {UNIT}.timer not installed"
    if _live_home():
        _systemctl("disable", "--now", f"{UNIT}.timer")
    for suffix in ("timer", "service"):
        (unit_dir() / f"{UNIT}.{suffix}").unlink(missing_ok=True)
    if _live_home():
        _systemctl("daemon-reload")
    return f"[OK] Removed {UNIT}.timer"


def start_now() -> bool:
    if not (installed() and _live_home()):
        return False
    try:
        subprocess.Popen(
            ["systemctl", "--user", "start", "--no-block", f"{UNIT}.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return True
