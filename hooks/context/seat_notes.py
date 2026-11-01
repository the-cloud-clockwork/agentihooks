"""The one line a swarm seated session gets on every user prompt: read the seat's learned notes before discovery."""

import os
from collections.abc import Mapping

REMINDER = (
    "SEAT NOTES: run `agentihooks swarm {slug} learned` before any tool call for context your seat may already hold."
)


def reminder(env: Mapping[str, str]) -> str:
    """The reminder for a session bound to a swarm seat, '' for any other session."""
    from scripts.swarm import naming

    slug = env.get("AGENTIHOOKS_SWARM")
    if not slug or not naming.lane_of(str(env.get("AGENTIHOOKS_AGENT_NAME"))):
        return ""
    return REMINDER.format(slug=slug)


def inject() -> None:
    """Inject the reminder on UserPromptSubmit; inject_context routes it into each target's context channel."""
    from hooks.common import inject_context

    line = reminder(os.environ)
    if line:
        inject_context(line)
