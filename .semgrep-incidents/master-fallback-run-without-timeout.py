import subprocess
import sys


def unbounded_pass() -> None:
    # ruleid: master-fallback-run-without-timeout
    subprocess.run([sys.executable, "-m", "scripts.swarm.master_fallback", "pass"], check=False)


def unbounded_reload() -> None:
    # ruleid: master-fallback-run-without-timeout
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)


def bounded_pass() -> None:
    # ok: master-fallback-run-without-timeout
    subprocess.run([sys.executable, "-m", "scripts.swarm.master_fallback", "pass"], check=False, timeout=50)


def bounded_reload() -> None:
    # ok: master-fallback-run-without-timeout
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=60)
