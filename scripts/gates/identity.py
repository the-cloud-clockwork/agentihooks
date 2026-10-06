"""The identity pin: in a swarm session, agentihooks ledger, swarm and msg act only as the session's own agent."""

import re
import shlex
from pathlib import PurePosixPath

from scripts.gates.base import Decision

PINNED_CLIS = frozenset({"ledger", "swarm", "msg"})
PINNED_VARS = frozenset({"AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM"})
AS_FLAGS = frozenset({"--a", "--as"})
CONTROL = frozenset(";&|()\n")
ASSIGNMENT = re.compile(r"^(?:AGENTIHOOKS_AGENT_NAME|AGENTIHOOKS_SWARM)=")
UNSET_PREFIX = re.compile(r"^(?:-u|--unset=)")
DECLARERS = frozenset({"unset", "export", "declare", "typeset"})
CLEARS_ENV = frozenset({"-i", "--ignore-environment", "-"})
ANY_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
WRAPPERS = frozenset({"env", "command", "exec", "sudo", "nohup", "time", "nice", "timeout"})
OPTION_ARGS = frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-n", "-k", "-s", "--signal"})


def _canonical(name):
    from scripts.swarm.naming import resolve_name

    return resolve_name(name)


def refusal(name, who):
    if not (name and who.pinned) or _canonical(name) == _canonical(who.name):
        return ""
    return f"this session is {who.name} in swarm {who.swarm} and cannot act as {name}: run the command with --as {who.name}"


def rewrite_refusal(who):
    return (
        f"this session is {who.name} in swarm {who.swarm}: a command that changes AGENTIHOOKS_AGENT_NAME or "
        "AGENTIHOOKS_SWARM cannot run agentihooks ledger, swarm or msg. Run it without the change."
    )


def _tokens(text):
    lexer = shlex.shlex(text, posix=True, punctuation_chars="".join(CONTROL))
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return text.split()


def _nests(current, token):
    return " " in token and bool(current) and (current[-1] == "-c" or PurePosixPath(current[0]).name == "eval")


def simple_commands(text):
    commands, current = [], []
    for token in _tokens(text):
        if token and CONTROL.issuperset(token):
            commands.append(current)
            current = []
            continue
        if _nests(current, token):
            commands.extend(simple_commands(token))
        current.append(token)
    commands.append(current)
    return [words for words in commands if words]


def program_index(words):
    for index, word in enumerate(words):
        if (
            ANY_ASSIGNMENT.match(word)
            or word.startswith("-")
            or (index and words[index - 1] in OPTION_ARGS)
            or DURATION.match(word)
            or PurePosixPath(word).name in WRAPPERS
        ):
            continue
        return index
    return None


def _pinned_args(words):
    index = program_index(words)
    if index is None or PurePosixPath(words[index]).name != "agentihooks":
        return None
    rest = words[index + 1 :]
    return rest[1:] if rest and rest[0] in PINNED_CLIS else None


def _as_names(args):
    for index, arg in enumerate(args):
        flag, eq, value = arg.partition("=")
        if flag in AS_FLAGS:
            yield value if eq else (args[index + 1] if index + 1 < len(args) else "")


def _rewrites(words):
    program = PurePosixPath(words[0]).name
    for index, word in enumerate(words):
        if ASSIGNMENT.match(word):
            return True
        if program == "env" and word in CLEARS_ENV:
            return True
        bare = UNSET_PREFIX.sub("", word)
        if bare not in PINNED_VARS:
            continue
        if bare != word or program in DECLARERS or (index and words[index - 1] in ("-u", "--unset")):
            return True
    return False


class PinnedIdentity:
    name = "identity"
    default_mode = "enforce"

    def matches(self, call):
        return call.tool == "Bash" and "agentihooks" in call.command

    def decide(self, call, who, state):
        if not who.pinned:
            return Decision()
        commands = simple_commands(call.command)
        invocations = [args for args in map(_pinned_args, commands) if args is not None]
        if invocations and any(_rewrites(words) for words in commands):
            return Decision.deny(rewrite_refusal(who))
        for args in invocations:
            for name in _as_names(args):
                if text := refusal(name, who):
                    return Decision.deny(text)
        return Decision()
