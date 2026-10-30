"""A scripted swarm agent for the feature suite, run as its own process with the launch env a real agent gets.

It names itself as a Claude harness process, reports its mounted profile canary through the binding command its
CLAUDE.md names after a set delay, beats like the session hooks, and hands off its seat through the agentihooks
command line when the test asks for it.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from scripts.profiles import binding
from scripts.swarm import idle
from scripts.swarm.store import connect

HARNESS = "claude"
PR_SET_NAME = 15
CANARY_DELAY = "FEATURE_AGENT_CANARY_DELAY_S"
BEAT_EVERY = "FEATURE_AGENT_BEAT_S"
COMMAND = re.compile(r"execute `(.+?)` through your shell tool")


def handoff_key(slug: str, name: str) -> str:
    """The Redis key a test writes a handoff document path to."""
    return f"feature-suite:{slug}:{name}:handoff"


def result_key(slug: str, name: str) -> str:
    """The Redis key the agent writes its handoff command result to."""
    return f"feature-suite:{slug}:{name}:handed-off"


def name_process(name: str) -> None:
    """Set this process's command name, which the binding canary walks up to as the harness."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NAME, name.encode(), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl PR_SET_NAME failed")


def canary_command(home: Path) -> list[str]:
    """The binding canary command the mounted persona tells the agent to run."""
    found = COMMAND.search((home / binding.PERSONAS[HARNESS]).read_text())
    if found is None:
        raise ValueError(f"no binding canary command in {home}")
    return shlex.split(found.group(1))


def agentihooks(*argv: str) -> subprocess.CompletedProcess:
    """Run the installed agentihooks command line beside this interpreter."""
    command = Path(sys.executable).with_name("agentihooks")
    return subprocess.run([str(command), *argv], capture_output=True, text=True, timeout=120)


def main() -> int:
    """Join the ledger, report the canary after its delay, beat, and hand off once asked."""
    name_process(HARNESS)
    slug, name = os.environ["AGENTIHOOKS_SWARM"], os.environ["AGENTIHOOKS_AGENT_NAME"]
    store = connect()
    joined = agentihooks("ledger", "--slug", slug, "--as", name, "join")
    if joined.returncode != 0:
        print(joined.stdout, joined.stderr, file=sys.stderr)
        return joined.returncode
    time.sleep(float(os.environ.get(CANARY_DELAY, "0")))
    reported = subprocess.run(canary_command(Path(os.environ["CLAUDE_CONFIG_DIR"])), capture_output=True, text=True)
    if reported.returncode != 0:
        print(reported.stdout, reported.stderr, file=sys.stderr)
        return reported.returncode
    every = float(os.environ.get(BEAT_EVERY, "0.5"))
    while (doc := store.redis.get(handoff_key(slug, name))) is None:
        idle.beat(store.redis, slug, name, idle.WORKING, int(time.time() * 1000))
        time.sleep(every)
    handed = agentihooks("swarm", slug, "handoff", doc)
    store.redis.set(
        result_key(slug, name),
        json.dumps({"returncode": handed.returncode, "stdout": handed.stdout, "stderr": handed.stderr}),
    )
    return handed.returncode


if __name__ == "__main__":
    sys.exit(main())
