"""The one line a swarm seated session gets on every user prompt: read the seat brief before discovery."""

import os
from collections.abc import Mapping

REMINDER = (
    "SEAT NOTES: run `agentihooks seat brief` before your first command: it prints the handoff, culture, latest "
    "recap and learned notes your seat holds."
)


def reminder(env: Mapping[str, str]) -> str:
    """The reminder for a session bound to a swarm seat, '' for any other session."""
    from scripts.swarm import naming

    slug = env.get("AGENTIHOOKS_SWARM")
    if not slug or not naming.lane_of(str(env.get("AGENTIHOOKS_AGENT_NAME"))):
        return ""
    return REMINDER


def inject() -> None:
    """Inject the reminder on UserPromptSubmit; inject_context routes it into each target's context channel."""
    from hooks.common import inject_context

    line = reminder(os.environ)
    if line:
        inject_context(line)
