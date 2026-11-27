"""Read the agentihooks commands a prompt, a skill, a rule or a docs page names and parse each with the parsers of the
command line itself, so guidance never names a command the command line refuses."""

import re
import shlex

from scripts.inbox.cli import build_parser as msg_parser
from scripts.seat_brief import build_parser as seat_parser
from scripts.swarm.cli import build_parser as swarm_parser

PARSERS = {
    "swarm": lambda argv: swarm_parser().parse_args(argv),
    "msg": lambda argv: msg_parser().parse_args(argv),
    "seat": lambda argv: seat_parser().parse_args(argv),
}


def named(text: str) -> list[str]:
    """Each agentihooks command a text names, in a code span or on a line of its own in a fenced block."""
    spans = re.findall(r"`(agentihooks [^`\n]+)`", text)
    return [*spans, *(line.strip() for line in text.splitlines() if line.startswith("agentihooks "))]


def typed(command: str) -> list[str]:
    """The command lines a named command stands for: one without its optional parts and, when it has any, one with
    them. A redirect is left out and each placeholder is filled."""
    shown = command.replace("\\|", "|").partition(" > ")[0]
    variants = dict.fromkeys((re.sub(r"\[[^\]]*\]", "", shown), re.sub(r"\[([^\]]*)\]", r"\1", shown)))
    return [re.sub(r"<[^>]+>", "x", variant) for variant in variants]


def taken(command: str) -> bool:
    """Whether the command line parses the named command, without its optional parts and with them, taking the first
    of each set of alternatives."""
    for line in typed(command):
        tool, *arguments = (token.split("|")[0] for token in shlex.split(line)[1:])
        if not PARSERS[tool](arguments):
            return False
    return True
