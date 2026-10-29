"""A headless worker run keeps the guardrails and drops swarm and fleet coordination output."""

import os

FLAG = "AGENTIHOOKS_WORKER_RUN"


def active(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(FLAG) == "1"
