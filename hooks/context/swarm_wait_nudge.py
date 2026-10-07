import os
import re
from collections.abc import Mapping

WORKERS = frozenset({"eng", "ci"})
WAIT_TOOLS = frozenset({"Bash", "Monitor"})
SHELL_WAIT_RE = re.compile(
    r"(?:^|[;&|({\n])\s*(?:sleep\s+\S|(?:until|while)\s[^;\n]*[;\n]\s*do\b"
    r"|gh\s+run\s+watch\b|gh\s+pr\s+checks\b[^;&|\n]*--watch)"
)


def is_wait(tool_name: str | None, tool_input: dict) -> bool:
    return tool_name in WAIT_TOOLS and bool(SHELL_WAIT_RE.search(str(tool_input.get("command"))))


def declared_wait(slug: str, name: str) -> dict | None:
    from scripts.swarm import idle
    from scripts.swarm.store import connect

    return idle.wait(connect().redis, slug, name)


def nudge(payload: dict, environ: Mapping[str, str] | None = None) -> str:
    from scripts.swarm.naming import lane_of

    env = os.environ if environ is None else environ
    slug, task, name = (env.get(k) for k in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK", "AGENTIHOOKS_AGENT_NAME"))
    if not (slug and task and name) or lane_of(name) not in WORKERS:
        return ""
    if not is_wait(payload.get("tool_name"), payload.get("tool_input") or {}) or declared_wait(slug, name):
        return ""
    return (
        f"=== SWARM WAIT ===\nYou hold task {task} and are starting a wait with no declared wait, which the health "
        f"check reads as a stale claim. Declare it first: agentihooks swarm {slug} wait --on checks <pr url> | reply "
        f'<inbox item> | task <id> when the tick can end it, else agentihooks swarm {slug} wait <minutes> --reason "<what '
        f'you wait on>" (at most 60) and agentihooks swarm {slug} progress --doing "<what>" --ends-when "<what>" at '
        "least every 30 minutes, because a bare wait alone still counts as quiet."
    )
