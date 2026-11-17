import subprocess
import sys


def bare_start(slug: str) -> int:
    # ruleid: master-fallback-bare-command
    return subprocess.Popen(["agentihooks", "swarm", slug, "master", "up", "--new"], start_new_session=True).pid


def bare_command(slug: str) -> list[str]:
    # ruleid: master-fallback-bare-command
    return ["agentihooks", "swarm", slug, "master", "up", "--replace"]


def interpreter_command(slug: str) -> list[str]:
    # ok: master-fallback-bare-command
    return [sys.executable, "-m", "scripts.install", "swarm", slug, "master", "up", "--replace"]
