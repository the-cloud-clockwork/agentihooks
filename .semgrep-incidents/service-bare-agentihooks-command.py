import subprocess
import sys


def bare_ensure() -> int:
    # ruleid: service-bare-agentihooks-command
    return subprocess.Popen(["agentihooks", "deps", "ensure", "--quiet"], start_new_session=True).pid


def bare_exit(slug: str, name: str) -> list[str]:
    # ruleid: service-bare-agentihooks-command
    return ["agentihooks", "swarm", slug, "--as", name, "exit"]


def interpreter_exit(slug: str, name: str) -> list[str]:
    # ok: service-bare-agentihooks-command
    return [sys.executable, "-P", "-m", "scripts.install", "swarm", slug, "--as", name, "exit"]
