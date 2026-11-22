"""Read the agentihooks commands a prompt, a skill, a rule or a docs page names and parse each with the parsers of the
command line itself, so guidance never names a command the command line refuses."""

import re
import shlex

from scripts.inbox.cli import build_parser as msg_parser
from scripts.swarm.cli import build_parser as swarm_parser

PARSERS = {"swarm": lambda argv: swarm_parser().parse_args(argv), "msg": lambda argv: msg_parser().parse_args(argv)}


def named(text: str) -> list[str]:
    """Each agentihooks command a text names, in a code span or on a line of its own in a fenced block."""
    spans = re.findall(r"`(agentihooks [^`\n]+)`", text)
    return [*spans, *(line.strip() for line in text.splitlines() if line.startswith("agentihooks "))]


def argv(command: str) -> tuple[str, list[str]]:
    """The tool and the arguments of a named command: an optional part and a redirect left out, the first of each set
    of alternatives taken and each placeholder filled."""
    typed = command.replace("\\|", "|").partition(" > ")[0]
    plain = re.sub(r"<[^>]+>", "x", re.sub(r"\[[^\]]*\]", "", typed))
    tokens = [token.split("|")[0] for token in shlex.split(plain)[1:]]
    return tokens[0], tokens[1:]


def taken(command: str) -> bool:
    """Whether the command line parses the named command."""
    tool, arguments = argv(command)
    return bool(PARSERS[tool](arguments))
