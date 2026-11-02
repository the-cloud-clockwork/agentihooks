"""The termination gate: a master that runs terminate-agent, or pkill or killall on claude or codex, acts only on the
operator's order. A bare kill names a pid, not an agent, so it is left to the master rule. It ships log only.
"""

from pathlib import PurePosixPath

from scripts.gates.base import Call, Decision, Who
from scripts.gates.identity import program_index, simple_commands
from scripts.swarm import naming

NAME = "terminations"
MASTER = "master"
KILLERS = frozenset({"pkill", "killall"})
AGENTS = ("claude", "codex")
INSPECTS = frozenset({"--list", "--dry-run"})
REFUSAL = (
    "termination: a master terminates, kills or retires an agent only on the operator's order. Answer him in your pane "
    "only when he writes, and leave the agent running until he says so."
)


def _terminates(words: list[str]) -> bool:
    index = program_index(words)
    if index is None:
        return False
    program, rest = PurePosixPath(words[index]).name, words[index + 1 :]
    if program in KILLERS:
        return any(agent in word for word in rest for agent in AGENTS)
    return program == "agentihooks" and rest[:1] == ["terminate-agent"] and not INSPECTS.intersection(rest)


def terminates(command: str) -> bool:
    return any(map(_terminates, simple_commands(command)))


class Terminations:
    name = NAME
    default_mode = "observe"

    def matches(self, call: Call) -> bool:
        return call.tool in ("Bash", "terminate-agent")

    def decide(self, call: Call, who: Who, state: object) -> Decision:
        if naming.lane_of(who.name) != MASTER or (call.tool == "Bash" and not terminates(call.command)):
            return Decision()
        return Decision.deny(REFUSAL)
