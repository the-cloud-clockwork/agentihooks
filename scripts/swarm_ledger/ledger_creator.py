"""Who may create a ledger or a swarm.

In the shared ledger folder only a master seat or the operator creates one, and only for at least three tasks unless
the operator asked. Any other folder takes any port, as the server allows; a swarm there needs its own Redis.
"""

MIN_TASKS = 3
CREATORS = ("master", "operator")
QUOTE_WORDS = 3
CALLER = (
    "only a master or the operator creates a ledger or a swarm in the shared ledger folder; run a proof on a "
    "scratch ledger folder (LEDGER_DIR)"
)
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

    return shared_directory(environ=environ)


def creator_refusal(environ) -> str:
    """Why this session may not create a ledger here; an empty string when it may."""
    return CALLER if shared(environ) and caller(environ) not in CREATORS else ""


def swarm_refusal(environ) -> str:
    """Why this session may not create a swarm here: a proof swarm also needs its own Redis."""
    from scripts.swarm.store import DEFAULT_URL

    refused = creator_refusal(environ)
    if refused or shared(environ):
        return refused
    return REDIS if environ.get("AGENTIHOOKS_SWARM_REDIS_URL", "") in ("", DEFAULT_URL) else ""


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
