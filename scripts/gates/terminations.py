"""The termination gate: a master that terminates or kills an agent acts only on the operator's order. It ships log
only.
"""

from pathlib import PurePosixPath

from scripts.gates.base import Decision
from scripts.gates.identity import program_index, simple_commands
from scripts.swarm import naming

NAME = "terminations"
MASTER = "master"
KILLERS = frozenset({"pkill", "killall"})
AGENTS = ("claude", "codex")
INSPECTS = frozenset({"--list", "--dry-run"})
REFUSAL = (
    "termination: a master terminates, kills or retires an agent only on the operator's order. Ask him in your pane "
    "or raise it in Priorities, and leave the agent running until he says so."
)


def _terminates(words):
    index = program_index(words)
    if index is None:
        return False
    program, rest = PurePosixPath(words[index]).name, words[index + 1 :]
    if program in KILLERS:
        return any(agent in word for word in rest for agent in AGENTS)
    return program == "agentihooks" and rest[:1] == ["terminate-agent"] and not INSPECTS.intersection(rest)


def terminates(command):
    return any(map(_terminates, simple_commands(command)))


class Terminations:
    name = NAME
    default_mode = "observe"

    def matches(self, call):
        return call.tool == "Bash"

    def decide(self, call, who, state):
        if naming.lane_of(who.name) != MASTER or not terminates(call.command):
            return Decision()
        return Decision.deny(REFUSAL)
