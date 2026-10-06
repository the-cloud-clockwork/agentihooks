"""Who may create a ledger or a swarm.

In the shared ledger folder only a master seat or the operator creates one, and only for at least three tasks unless
the operator asked. Anywhere else is a proof: a scratch ledger folder on a spare port, and for a swarm its own Redis.
"""

from pathlib import Path

MIN_TASKS = 3
SHARED_PORT = "8765"
CREATORS = ("master", "operator")
QUOTE_WORDS = 3
CALLER = (
    "only a master or the operator creates a ledger or a swarm in the shared ledger folder; run a proof on a "
    "scratch ledger folder (LEDGER_DIR) with a spare port (LEDGER_PORT)"
)
PORT = "a ledger in a scratch folder needs a spare port: set LEDGER_PORT to a free port other than 8765"
REDIS = "a swarm on a scratch ledger folder needs its own Redis: set AGENTIHOOKS_SWARM_REDIS_URL to a proof Redis"
FLOOR = (
    "a ledger or swarm in the shared ledger folder needs at least {need} tasks and this one has {have}; pass "
    "--operator-asked with the operator's words asking for it"
)


def caller(environ):
    if not environ.get("AGENTIHOOKS_SWARM"):
        return "operator"
    return environ.get("AGENTIHOOKS_SWARM_LANE")


def operator_asked(environ, quote) -> bool:
    if len(str(quote).split()) < QUOTE_WORDS:
        return False
    name = environ.get("AGENTIHOOKS_AGENT_NAME")
    if not name:
        return caller(environ) == "operator"
    from hooks.context import operator_words

    return bool(operator_words.matching(name, quote))


def shared(environ) -> bool:
    from scripts.swarm_ledger.ledger_link import shared_directory

    return shared_directory(Path(environ.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser())


def creator_refusal(environ, swarm) -> str:
    """Why this session may not create a ledger, or a swarm when `swarm`, here; an empty string when it may."""
    from scripts.swarm.store import DEFAULT_URL

    if shared(environ):
        return "" if caller(environ) in CREATORS else CALLER
    if (environ.get("LEDGER_PORT") or SHARED_PORT) == SHARED_PORT:
        return PORT
    if swarm and environ.get("AGENTIHOOKS_SWARM_REDIS_URL", "") in ("", DEFAULT_URL):
        return REDIS
    return ""


def floor_refusal(environ, tasks, asked) -> str:
    """Why a ledger or swarm in the shared folder holds too few tasks; an empty string when it holds enough."""
    if tasks >= MIN_TASKS or not shared(environ) or operator_asked(environ, asked):
        return ""
    return FLOOR.format(need=MIN_TASKS, have=tasks)


def content_tasks(content) -> int:
    """A new ledger's least task count: its listed tasks, or one per phase when there are more phases."""
    return max(len(content.get("tasks", [])), len(content.get("phases", [])))


def swarm_tasks(doc) -> int:
    """A swarm ledger's tasks, counting each automatic phase still waiting for its planner as one."""
    tasks = doc.get("tasks", [])
    planned = {task.get("phase") for task in tasks}
    waiting = [p for p in doc.get("phases", []) if p.get("planning") == "auto" and p.get("id") not in planned]
    return len(tasks) + len(waiting)
