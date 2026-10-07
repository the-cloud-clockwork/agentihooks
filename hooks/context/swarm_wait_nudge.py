import os
import re

WORKERS = frozenset({"eng", "ci"})
WAIT_TOOLS = frozenset({"Monitor"})
SHELL_WAIT_RE = re.compile(r"(?:^|[\s;&|(])(?:sleep\s+\d|(?:until|while)\s.*?[;\n]\s*do\b)", re.S)


def is_wait(tool_name, tool_input):
    if tool_name in WAIT_TOOLS:
        return True
    return tool_name == "Bash" and bool(SHELL_WAIT_RE.search(str(tool_input.get("command"))))


def declared_wait(slug, name):
    from scripts.swarm import idle
    from scripts.swarm.store import connect

    return idle.wait(connect().redis, slug, name)


def nudge(payload, environ=None):
    from scripts.swarm.naming import lane_of

    env = os.environ if environ is None else environ
    slug, task, name = (env.get(k) for k in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK", "AGENTIHOOKS_AGENT_NAME"))
    if not (slug and task and name) or lane_of(name) not in WORKERS:
        return ""
    if not is_wait(payload.get("tool_name"), payload.get("tool_input") or {}) or declared_wait(slug, name):
        return ""
    return (
        f"=== SWARM WAIT ===\nYou hold task {task} and are starting a wait with no declared wait, so the health check "
        f'will read it as a stale claim. Declare it first: agentihooks swarm {slug} wait <minutes> --reason "<what you '
        f'wait on>", or agentihooks swarm {slug} wait --on checks <pr url> | reply <inbox item> | task <id>.'
    )
