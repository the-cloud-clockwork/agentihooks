import os
import pwd
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent
LINKS = ("guard-daemon.sh", "guard-config.json")
UNITS = ("guard-daemon.service", "guard-daemon.timer")
TIMER = "guard-daemon.timer"


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


def drift(home: Path) -> list[str]:
    found = []
    for name in LINKS:
        path = home / name
        if not path.is_symlink() and not path.exists():
            found.append(f"{path} is missing")
        elif not path.is_symlink() or path.resolve() != (REPO / name).resolve():
            found.append(f"{path} is not a link to {REPO / name}")
    for name in UNITS:
        path = _unit_dir(home) / name
        if not path.exists():
            found.append(f"{path} is missing")
        elif path.read_text() != (REPO / name).read_text():
            found.append(f"{path} differs from {REPO / name}")
    return found


def install(home: Path, enable: bool) -> str:
    for name in LINKS:
        staged = home / f".{name}.agentihooks"
        staged.unlink(missing_ok=True)
        staged.symlink_to(REPO / name)
        staged.replace(home / name)
    _unit_dir(home).mkdir(parents=True, exist_ok=True)
    for name in UNITS:
        (_unit_dir(home) / name).write_text((REPO / name).read_text())
    if _live(home) and not (_systemctl("daemon-reload") and (not enable or _systemctl("enable", "--now", TIMER))):
        return f"[!!] Host guard files installed in {home}; systemctl --user could not reload or enable {TIMER}"
    return f"[OK] Host guard installed in {home} from {REPO}"


def reconcile(home: Path) -> str:
    paths = [home / name for name in LINKS] + [_unit_dir(home) / name for name in UNITS]
    if not any(path.is_symlink() or path.exists() for path in paths):
        return "[--] Host guard not installed; agentihooks host-guard install adds it"
    if not drift(home):
        return f"[OK] Host guard in sync with {REPO}"
    return install(home, enable=False)


def run(action: str, home: Path) -> int:
    if action == "install":
        message = install(home, enable=True)
        print(message)
        return 0 if message.startswith("[OK]") else 1
    found = drift(home)
    print("\n".join(found) if found else f"[OK] Host guard in sync with {REPO}")
    return 1 if found else 0
