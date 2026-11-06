import subprocess
import sys


def unbounded_pass():
    # ruleid: master-fallback-run-without-timeout
    subprocess.run([sys.executable, "-m", "scripts.swarm.master_fallback", "pass"], check=False)


def unbounded_reload():
    # ruleid: master-fallback-run-without-timeout
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)


def bounded_pass():
    # ok: master-fallback-run-without-timeout
    subprocess.run([sys.executable, "-m", "scripts.swarm.master_fallback", "pass"], check=False, timeout=50)


def bounded_reload():
    # ok: master-fallback-run-without-timeout
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=60)
