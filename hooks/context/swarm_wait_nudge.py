import os
import re
from collections.abc import Mapping

WORKERS = frozenset({"eng", "ci"})
WAIT_TOOLS = frozenset({"Bash", "Monitor"})
NATIVE_WAITS = frozenset({"sleep", "wait"})
SHELL_WAIT_RE = re.compile(
    r"(?:^|[;&|({\n])\s*(?:(?:do|then|else|nohup|timeout\s+\S+)\s+)*(?:sleep\s+\S|(?:until|while)\s[^;\n]*[;\n]\s*do\b"
    r"|gh\s+run\s+watch\b|gh\s+pr\s+checks\b[^;&|\n]*--watch)"
)


def is_wait(tool_name: str | None, tool_input: dict) -> bool:
    if tool_name in NATIVE_WAITS:
        return True
    if tool_name == "write_stdin":
        return not tool_input.get("chars")
    if tool_name not in WAIT_TOOLS:
        return False
    command = str(tool_input.get("command"))
    return any(SHELL_WAIT_RE.search(text) for text in (command, *_nested_commands(command)))


def _nested_commands(program: str) -> list[str]:
    from hooks.targets.normalizer import exec_calls

    return [call["cmd"] for call in exec_calls(program) if call.get("cmd")]


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


def _github_watch(tokens: list[str]) -> bool:
    from pathlib import Path

    if not tokens or Path(tokens[0]).name != "gh":
        return False
    args = tokens[1:]
    while args and args[0].startswith("-"):
        args = args[2:] if args[0] in ("-R", "--repo", "--hostname") else args[1:]
    return args[:2] == ["run", "watch"] or (
        args[:2] == ["pr", "checks"] and any(flag in ("--watch", "--watch=true") for flag in args[2:])
    )


def check_github_watch(payload: dict) -> None:
    from hooks.context.shell_commands import commands
    from hooks.hook_manager import BlockAction
    from scripts.swarm.naming import lane_of

    slug, name = (os.environ.get(key) for key in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_NAME"))
    if not slug or not name or lane_of(name) not in WORKERS or payload.get("tool_name") not in WAIT_TOOLS:
        return
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or tool_input.get("cmd")
    if command and any(_github_watch(tokens) for tokens in commands(command)):
        raise BlockAction(
            "Independent GitHub watches spend the shared GitHub hourly quota. "
            f"Use agentihooks swarm {slug} wait --on checks <pr url>, "
            "or wait --on mutation <run url> for Mutation preflight. The swarm tick resolves these waits centrally."
        )
