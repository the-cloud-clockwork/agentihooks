"""The quiet claim gate: a worker holding a claim with no progress signal for 30 minutes must say what it is doing.

The tick raises the flag as the verdict file gates/quiet/<agent>, which a bash shim tests on every tool call. While it
stands every call is refused except the ledger, swarm and msg commands, until `swarm <slug> progress` clears it.
"""

from datetime import datetime, timezone

from scripts.gates import log
from scripts.gates.base import Decision, Who
from scripts.gates.identity import _pinned_args, simple_commands
from scripts.gates.progress import Progress
from scripts.gates.verdicts import Verdicts
from scripts.swarm_ledger import ledger_workspace

NAME = "quiet"
QUIET_MINUTES = 30
MINUTE_MS = 60_000
STATUS = "status line"
WORKERS = frozenset({"eng", "ci"})
HELD = frozenset({"claimed", "pr"})
GONE = frozenset({"finished", "awaiting-decision"})


def written_at(slug, task_id):
    try:
        return int((ledger_workspace.folder(slug, task_id) / "progress.md").stat().st_mtime * 1000)
    except (OSError, ValueError):
        return 0


def last_signal(redis, slug, agent):
    return max(Progress(redis, slug).read(agent.name).outcome_at, written_at(slug, agent.task), agent.started_at)


def checked_wait(redis, slug, name, now_ms):
    from scripts.swarm import idle

    held = idle.wait(redis, slug, name)
    return bool(held and held.get("on") and held["until"] > now_ms)


def holding(agent, rows):
    task = rows.get(agent.task, {})
    return (
        agent.lane in WORKERS
        and agent.state not in GONE
        and task.get("state") in HELD
        and task.get("claimed_by") == agent.name
    )


def quiet_minutes(redis, slug, agents, rows, now_ms):
    """Minutes each worker holding its claim has gone without a progress signal; a checked wait is never quiet, and
    an agent with no signal at all, not even a start time, is not measured."""
    found = {}
    for agent in agents:
        if not holding(agent, rows) or checked_wait(redis, slug, agent.name, now_ms):
            continue
        signal = last_signal(redis, slug, agent)
        if signal:
            found[agent.name] = max(0, now_ms - signal) // MINUTE_MS
    return found


def refusal(slug, task_id, minutes):
    return (
        f"quiet for {minutes} minutes on task {task_id}: run agentihooks swarm {slug} progress --doing <what> "
        "--ends-when <what> to say what you are doing and when it ends"
    )


def _flagged(flags):
    folder = flags.path("_").parent
    return {path.name for path in folder.iterdir() if not path.name.startswith(".")} if folder.is_dir() else set()


def quiet_pass(store, slug, rows, now_ms, home=None):
    agents, flags, actions = store.agents(slug), Verdicts(slug, NAME, home), []
    quiet = quiet_minutes(store.redis, slug, agents, rows, now_ms)
    for agent in agents:
        minutes = quiet.get(agent.name, 0)
        if minutes < QUIET_MINUTES or flags.read(agent.name) is not None:
            continue
        reason = refusal(slug, agent.task, minutes)
        flags.write(agent.name, NAME, reason, now_ms)
        who = Who(name=agent.name, swarm=slug, lane=agent.lane, task=agent.task)
        log.append(slug, log.Row.of(NAME, "count", who, reason=reason, now_ms=now_ms), home)
        actions.append(
            f"raised the quiet flag on {agent.name}: {minutes} minutes with no progress on task {agent.task}"
        )
    for name in sorted(_flagged(flags)):
        if quiet.get(name, 0) < QUIET_MINUTES:
            flags.clear(name)
            actions.append(f"cleared the quiet flag on {name}")
    return actions


def report(store, ledger, slug, agent, doing, ends_when, now_ms, home=None):
    line = f"Doing {doing}. Done when {ends_when}."
    Progress(store.redis, slug).outcome(agent.name, STATUS, now_ms)
    ledger.comment(slug, agent.task, line, agent.name)
    path = ledger_workspace.folder(slug, agent.task) / "progress.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        out.write(f"{datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat()} {line}\n")
    Verdicts(slug, NAME, home).clear(agent.name)
    return line


def allowed(call):
    commands = simple_commands(call.command) if call.tool == "Bash" else []
    return bool(commands) and all(_pinned_args(words) is not None for words in commands)


class QuietClaim:
    name = NAME
    default_mode = "enforce"

    def matches(self, call):
        return bool(call.tool)

    def decide(self, call, who, state):
        if not who.pinned:
            return Decision()
        flag = state.read(who.name)
        if flag is None or allowed(call):
            return Decision()
        return Decision.deny(flag["reason"])
