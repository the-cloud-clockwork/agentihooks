import os
import re
import shlex
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hooks.config import AGENTIHOOKS_HOME, COMPACT_LIMIT, HANDOFF_MARGIN
from hooks.context.context_usage import used_tokens
from scripts.codex_context import codex_context
from scripts.handoff import check as handoff_check
from scripts.swarm import naming

_INSTRUCTIONS = (
    "in Handoff v2 form: the line # Handoff v2, then ## Intent, ## Done, ## Stopped at, ## Decisions and promises, "
    "## Next and ## Read first, each once and in that order, None under a heading with nothing to say, and "
    "<!-- handoff complete --> as the last line. Every Done bullet carries its evidence or the word hypothesis; "
    "Read first lists addresses (an issue or pull request link, ledger:<slug>/<kind>/<id>, workspace:<task>/progress, "
    "recap:<seat>, inbox:<id>), each with the question it answers; no file paths, line numbers or credential values. "
    "Write it and a recap (what you did, where you stopped, what you "
    "promised) to files under ~/scratchpad, record any lesson for your seat with "
    '`agentihooks swarm {slug} learned "<lesson>"`, then run `agentihooks swarm {slug} handoff <doc> --recap <recap>` '
    "and stop. A successor continues the task from them."
)
_DIRECTIVE = (
    "CONTEXT RECYCLE — this session holds {used}k tokens, at or over the {limit}k limit. Write a handoff document "
    + _INSTRUCTIONS
)

_ALLOWED = (
    " Until the handoff every tool call is denied except reading files (cat, head, tail, ls, wc, grep and git status, "
    "log, diff, show in the shell), writing the handoff document under "
    "~/scratchpad, `agentihooks swarm {slug} handoff <doc> [--recap <recap>]`, `agentihooks swarm {slug} learned` "
    "and `agentihooks ledger` comment, say, leave and ack, "
    "each as one command."
)

_READ_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "NotebookRead"})
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})
_LEDGER_STEPS = frozenset({"comment", "say", "leave", "ack"})
_READ_COMMANDS = frozenset({"cat", "head", "tail", "ls", "wc", "grep"})
_READ_GIT = frozenset({"status", "log", "diff", "show"})
_SUBSTITUTION = ("`", "$(", "<(", ">(", "\n")
_PATCH_TARGET = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.MULTILINE)


def _used(session_id: str) -> int | None:
    used = used_tokens(session_id)
    if used is None:
        codex = codex_context(session_id)
        used = codex.used if codex else None
    return used


def _overrun(session_id: str, environ) -> tuple[str, int] | None:
    slug = _swarm_of(os.environ if environ is None else environ)
    used = _used(session_id) if slug and session_id else None
    if used is None or used < _gate_limit() * 1000:
        return None
    return slug, used


def _gate_limit() -> int:
    return COMPACT_LIMIT + HANDOFF_MARGIN


def _swarm_of(environ) -> str:
    name = environ.get("AGENTIHOOKS_AGENT_NAME", "")
    if not naming.lane_of(name) or environ.get("AGENTIHOOKS_SWARM_LANE") == naming.OPERATOR:
        return ""
    return naming.legacy_slug(name) or environ.get("AGENTIHOOKS_SWARM", "")


def over_limit(session_id: str, environ=None) -> str | None:
    overrun = _overrun(session_id, environ)
    return overrun[0] if overrun else None


def directive(session_id: str, environ=None, now: datetime | None = None) -> str | None:
    slug = _swarm_of(os.environ if environ is None else environ)
    used = _used(session_id) if slug and session_id else None
    if used is None or used < COMPACT_LIMIT * 1000:
        return None
    hard = used >= _gate_limit() * 1000
    stage = "recycle" if hard else "prepare"
    marker = AGENTIHOOKS_HOME / "context_usage" / f"{session_id}.{stage}"
    if marker.exists():
        return None
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    if hard:
        return _DIRECTIVE.format(used=used // 1000, limit=_gate_limit(), slug=slug)
    deadline = (now or datetime.now(timezone.utc)) + timedelta(minutes=25)
    return (
        f"HANDOFF PREPARATION — this session holds {used // 1000}k tokens. Write your handoff now. "
        f"Deadline: {deadline:%Y-%m-%d %H:%M UTC}, or before the {_gate_limit()}k hard gate, whichever comes first. "
        + _INSTRUCTIONS.format(slug=slug)
    )


def _in_scratchpad(path: str) -> bool:
    root = os.path.realpath(Path.home() / "scratchpad")
    return os.path.realpath(os.path.expanduser(path)).startswith(root + os.sep)


def _handoff_write(tool_input: dict) -> bool:
    paths = [str(tool_input.get("file_path") or "")]
    paths += [added or moved for added, moved in _PATCH_TARGET.findall(str(tool_input.get("content") or ""))]
    return all(path and _in_scratchpad(path.strip()) for path in paths)


def _ledger_step(args: list[str]) -> str | None:
    rest = iter(args)
    for token in rest:
        if not token.startswith("--"):
            return token
        if "=" not in token:
            next(rest, None)
    return None


def _single_command(command: str) -> list[str] | None:
    if any(mark in command for mark in _SUBSTITUTION):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens or any(set(token) <= set(lexer.punctuation_chars) for token in tokens):
        return None
    return tokens


def _shell_read(tokens: list[str]) -> bool:
    if tokens[0] in _READ_COMMANDS:
        return True
    return (
        tokens[0] == "git"
        and len(tokens) > 1
        and tokens[1] in _READ_GIT
        and not any(token.startswith("--output") for token in tokens)
    )


def _allowed_command(command: str, slug: str) -> bool:
    tokens = _single_command(command)
    if tokens is None:
        return False
    if _shell_read(tokens):
        return True
    if len(tokens) < 3 or os.path.basename(tokens[0]) != "agentihooks":
        return False
    if tokens[1] == "swarm":
        return _swarm_step(tokens[2:], slug)
    return tokens[1] == "ledger" and _ledger_step(tokens[2:]) in _LEDGER_STEPS


def _swarm_step(args: list[str], slug: str) -> bool:
    if args[:2] == [slug, "learned"]:
        return len(args) == 3 or (len(args) == 5 and args[3] == "--maturity")
    return args[:2] == [slug, "handoff"] and (len(args) == 3 or (len(args) == 5 and args[3] == "--recap"))


def gate(tool_name: str, tool_input: dict, session_id: str, environ=None) -> str | None:
    overrun = _overrun(session_id, environ)
    if overrun is None or tool_name in _READ_TOOLS:
        return None
    slug, used = overrun
    if tool_name in _WRITE_TOOLS and _handoff_write(tool_input):
        return None
    command = str(tool_input.get("command") or "")
    if tool_name == "Bash" and _allowed_command(command, slug):
        return _handoff_refusal(command, slug)
    return "BLOCKED: " + (_DIRECTIVE + _ALLOWED).format(used=used // 1000, limit=_gate_limit(), slug=slug)


def _resolver(slug: str):
    from scripts.handoff.resolve import Resolver
    from scripts.swarm.store import SwarmError, connect

    try:
        redis = connect().redis
    except SwarmError:
        redis = None
    return Resolver(slug, redis)


def _handoff_refusal(command: str, slug: str) -> str | None:
    tokens = _single_command(command) or []
    if tokens[1:2] != ["swarm"] or tokens[3:4] != ["handoff"]:
        return None
    try:
        text = Path(tokens[4]).expanduser().read_text(encoding="utf-8")
    except OSError:
        return None
    found = handoff_check.problems(text, _resolver(slug))
    return "BLOCKED: " + handoff_check.refusal(found) if found else None
