"""The one line a swarm seated session gets on every user prompt: read the seat's learned notes before discovery."""

import os
from collections.abc import Mapping

from scripts.swarm import naming

REMINDER = (
    "SEAT NOTES: run `agentihooks swarm {slug} learned` before any tool call for context your seat may already hold."
)


def reminder(environ: Mapping[str, str] | None = None) -> str:
    """The reminder for a session bound to a swarm seat, '' for any other session."""
    env = os.environ if environ is None else environ
    slug = env.get("AGENTIHOOKS_SWARM", "")
    if not slug or not naming.lane_of(env.get("AGENTIHOOKS_AGENT_NAME", "")):
        return ""
    return REMINDER.format(slug=slug)


def inject() -> None:
    """Inject the reminder on UserPromptSubmit through the target's context channel."""
    from hooks.common import inject_context
    from hooks.targets.capabilities import can_inject_context

    line = reminder()
    if line and can_inject_context("UserPromptSubmit"):
        inject_context(line, also_log=False, skip_compression=True, kind="refocus")
