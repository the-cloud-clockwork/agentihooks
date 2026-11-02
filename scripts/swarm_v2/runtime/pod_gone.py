"""Whether kubectl confirms that no pod carries an execution's label: any failure, timeout or listed pod is unconfirmed."""

import json
import subprocess
from collections.abc import Callable

from scripts.swarm_v2.kubernetes.watch import EXECUTION_LABEL

TIMEOUT_SECONDS = 10
Runner = Callable[..., subprocess.CompletedProcess]


def absent(execution_id: str, run: Runner = subprocess.run) -> bool:
    """True only when kubectl answers an empty pod listing for the execution in every namespace."""
    if not execution_id:
        return False
    command = [
        "kubectl",
        "get",
        "pods",
        "--all-namespaces",
        "--selector",
        f"{EXECUTION_LABEL}={execution_id}",
        "--output",
        "json",
    ]
    try:
        done = run(command, capture_output=True, timeout=TIMEOUT_SECONDS, check=False)
        answer = json.loads(done.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    return done.returncode == 0 and isinstance(answer, dict) and answer.get("items") == []
