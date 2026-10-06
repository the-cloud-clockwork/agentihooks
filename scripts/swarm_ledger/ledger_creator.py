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


def caller(environ) -> str:
    if not environ.get("AGENTIHOOKS_SWARM"):
        return "operator"
    return environ.get("AGENTIHOOKS_SWARM_LANE") or "agent"


def operator_asked(environ, quote) -> bool:
    if len(str(quote).split()) < QUOTE_WORDS:
        return False
    name = environ.get("AGENTIHOOKS_AGENT_NAME")
    if not name:
        return caller(environ) == "operator"
    from hooks.context import operator_words

    return bool(operator_words.matching(name, quote))


def _scratch_refusal(environ, swarm) -> str:
    from scripts.swarm.store import DEFAULT_URL

    if (environ.get("LEDGER_PORT") or SHARED_PORT) == SHARED_PORT:
        return PORT
    if swarm and environ.get("AGENTIHOOKS_SWARM_REDIS_URL", "") in ("", DEFAULT_URL):
        return REDIS
    return ""


def refusal(environ, tasks, asked="", swarm=False, floor=True, folder=None) -> str:
    """Why this session may not create a ledger (or a swarm) holding this many tasks, or an empty string."""
    from scripts.swarm_ledger.ledger_link import shared_directory

    folder = folder or Path(environ.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser()
    if not shared_directory(Path(folder)):
        return _scratch_refusal(environ, swarm)
    if caller(environ) not in CREATORS:
        return CALLER
    if floor and tasks < MIN_TASKS and not operator_asked(environ, asked):
        return FLOOR.format(need=MIN_TASKS, have=tasks)
    return ""


def content_tasks(content) -> int:
    """A new ledger's least task count: its listed tasks, or one per phase when there are more phases."""
    return max(len(content.get("tasks", [])), len(content.get("phases", [])))


def swarm_tasks(doc) -> int:
    """A swarm ledger's tasks, counting each automatic phase still waiting for its planner as one."""
    tasks = doc.get("tasks", [])
    planned = {task.get("phase") for task in tasks}
    waiting = [p for p in doc.get("phases", []) if p.get("planning") == "auto" and p.get("id") not in planned]
    return len(tasks) + len(waiting)
