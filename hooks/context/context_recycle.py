import os
import re

from hooks.config import AGENTIHOOKS_HOME, COMPACT_LIMIT
from hooks.context.context_usage import used_tokens
from scripts.codex_context import codex_context

_AGENT_NAME = re.compile(r"^(?P<slug>[a-z][a-z0-9-]*)-(?:eng|ci|master)-\d+$")

_DIRECTIVE = (
    "CONTEXT RECYCLE — this session holds {used}k tokens, at or over the {limit}k limit. Write a handoff document "
    "(task state, what is done, what is red, the next step) to a file under ~/scratchpad, then run "
    "`agentihooks swarm {slug} handoff <doc>` and stop. A successor continues the task from the document."
)


def _used(session_id: str) -> int | None:
    used = used_tokens(session_id)
    if used is None:
        codex = codex_context(session_id)
        used = codex.used if codex else None
    return used


def directive(session_id: str, environ=None) -> str | None:
    match = _AGENT_NAME.match((os.environ if environ is None else environ).get("AGENTIHOOKS_AGENT_NAME", ""))
    used = _used(session_id) if match and session_id else None
    if used is None or used < COMPACT_LIMIT * 1000:
        return None
    marker = AGENTIHOOKS_HOME / "context_usage" / f"{session_id}.recycle"
    if marker.exists():
        return None
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return _DIRECTIVE.format(used=used // 1000, limit=COMPACT_LIMIT, slug=match["slug"])
