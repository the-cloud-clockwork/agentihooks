"""The quiet claim gate: a worker holding a claim with no progress signal for 30 minutes must say what it is doing.

The tick raises the flag as the verdict file gates/quiet/<agent>, which a bash shim tests on every tool call. While it
stands every call is refused except the ledger, swarm and msg commands, until `swarm <slug> progress` clears it.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.gates import log
from scripts.gates.base import Call, Decision, Who
from scripts.gates.identity import _pinned_args, simple_commands
from scripts.gates.progress import Progress
from scripts.gates.verdicts import Verdicts
from scripts.swarm.store import AgentRecord
from scripts.swarm_ledger import ledger_workspace

NAME = "quiet"
QUIET_MINUTES = 30
MINUTE_MS = 60_000
STATUS = "status line"
WORKERS = frozenset({"eng", "ci"})
HELD = frozenset({"claimed", "pr"})
GONE = frozenset({"finished", "awaiting-decision"})


def written_at(slug: str, task_id: str) -> int:
    try:
        return int((ledger_workspace.folder(slug, task_id) / "progress.md").stat().st_mtime * 1000)
    except (OSError, ValueError):
        return 0


def last_signal(redis: Any, slug: str, agent: AgentRecord) -> int:
    from scripts.swarm import idle

    return max(
        Progress(redis, slug).read(agent.name).outcome_at,
        written_at(slug, agent.task),
        agent.started_at,
        idle.waited(redis, slug, agent.name),
    )


def declared_wait(redis: Any, slug: str, name: str, now_ms: int) -> bool:
    from scripts.swarm import idle

    held = idle.wait(redis, slug, name)
    return bool(held and held["until"] > now_ms and idle.named(held))


def holding(agent: AgentRecord, rows: dict[str, dict[str, Any]]) -> bool:
    task = rows.get(agent.task, {})
    return (
        agent.lane in WORKERS
        and agent.state not in GONE
        and task.get("state") in HELD
        and task.get("claimed_by") == agent.name
    )


def quiet_minutes(
    redis: Any, slug: str, agents: Iterable[AgentRecord], rows: dict[str, dict[str, Any]], now_ms: int
) -> dict[str, int]:
    """Minutes each worker holding its claim has gone without a progress signal; a declared wait is never quiet and
    its end restarts the clock, and an agent with no signal at all, not even a start time, is not measured."""
    found: dict[str, int] = {}
    for agent in agents:
        if not holding(agent, rows) or declared_wait(redis, slug, agent.name, now_ms):
            continue
        signal = last_signal(redis, slug, agent)
        if signal:
            found[agent.name] = (now_ms - signal) // MINUTE_MS
    return found


def refusal(slug: str, task_id: str, minutes: int) -> str:
    return (
        f"quiet for {minutes} minutes on task {task_id}: run agentihooks swarm {slug} progress --doing <what> "
        "--ends-when <what> to say what you are doing and when it ends"
    )


def _flagged(slug: str, home: str | Path | None) -> set[str]:
    folder = log.gates_dir(slug, home) / NAME
    return {path.name for path in folder.iterdir() if not path.name.startswith(".")} if folder.is_dir() else set()


def quiet_pass(
    store: Any, slug: str, rows: dict[str, dict[str, Any]], now_ms: int, home: str | Path | None = None
) -> list[str]:
    agents, flags, actions = {a.name: a for a in store.agents(slug)}, Verdicts(slug, NAME, home), list[str]()
    quiet = quiet_minutes(store.redis, slug, agents.values(), rows, now_ms)
    standing = {name for name, minutes in quiet.items() if minutes >= QUIET_MINUTES}
    for name in sorted(standing):
        if flags.read(name) is not None:
            continue
        agent, minutes = agents[name], quiet[name]
        reason = refusal(slug, agent.task, minutes)
        flags.write(name, NAME, reason, now_ms)
        log.append(slug, log.Row.of(NAME, "count", Who(name=name, task=agent.task), reason=reason, now_ms=now_ms), home)
        actions.append(f"raised the quiet flag on {name}: {minutes} minutes with no progress on task {agent.task}")
    for name in sorted(_flagged(slug, home) - standing):
        flags.clear(name)
        actions.append(f"cleared the quiet flag on {name}")
    return actions


@dataclass(frozen=True)
class Status:
    doing: str
    ends_when: str

    @property
    def line(self):
        return f"Doing {self.doing}. Done when {self.ends_when}."


def report(
    store: Any,
    ledger: Any,
    slug: str,
    agent: AgentRecord,
    status: "Status",
    now_ms: int,
    home: str | Path | None = None,
) -> str:
    line = status.line
    Progress(store.redis, slug).outcome(agent.name, STATUS, now_ms)
    ledger.comment(slug, agent.task, line, agent.name)
    path = ledger_workspace.folder(slug, agent.task) / "progress.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as out:
        out.write(f"{datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat()} {line}\n".encode())
    Verdicts(slug, NAME, home).clear(agent.name)
    return line


def allowed(call: Call) -> bool:
    commands = simple_commands(call.command) if call.tool == "Bash" else []
    return bool(commands) and all(_pinned_args(words) is not None for words in commands)


class QuietClaim:
    name = NAME
    default_mode = "enforce"

    def matches(self, call: Call) -> bool:
        return bool(call.tool)

    def decide(self, call: Call, who: Who, state: Verdicts) -> Decision:
        if not who.pinned:
            return Decision()
        flag = state.read(who.name)
        if flag is None or allowed(call):
            return Decision()
        return Decision.deny(flag["reason"])
