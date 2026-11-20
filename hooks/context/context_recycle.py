import json
import os
import re
import shlex
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hooks.config import AGENTIHOOKS_HOME, COMPACT_LIMIT, HANDOFF_MARGIN
from hooks.context.context_usage import used_tokens
from scripts.codex_context import codex_context
from scripts.handoff import check as handoff_check
from scripts.quick import handoff as quick_handoff
from scripts.quick import paths as quick_paths
from scripts.quick.member import Member
from scripts.quick.member import find as find_member
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
_HEAD = "CONTEXT RECYCLE — this session holds {used}k tokens, at or over the {limit}k limit. Write a handoff document "
_DIRECTIVE = _HEAD + _INSTRUCTIONS
_QUICK_INSTRUCTIONS = (
    "in Handoff v2 form: the line # Handoff v2, then ## Intent, ## Done, ## Stopped at, ## Decisions and promises, "
    "## Next and ## Read first, each once and in that order, None under a heading with nothing to say, and "
    "<!-- handoff complete --> as the last line. Every Done bullet carries its evidence or the word hypothesis; "
    "Read first lists addresses (an issue or pull request link, seat:<seat>), each with the question it answers; "
    "no file paths, line numbers or credential values. Write it to {handoff}, put any lesson for your seat in "
    "{learned}, then run `agentihooks quick handoff` and stop. Your successor starts in this same pane from it."
)
_QUICK_ALLOWED = (
    " Until the handoff every tool call is denied except reading files (cat, head, tail, ls, wc, grep and git status, "
    "log, diff, show in the shell), writing under ~/scratchpad and `agentihooks quick handoff`, each as one command."
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
    slug = _swarm_of(os.environ if environ is None else environ, session_id)
    used = _used(session_id) if slug and session_id else None
    if used is None or used < _gate_limit() * 1000:
        return None
    return slug, used


def _gate_limit(compact_limit: int | None = None) -> int:
    return (COMPACT_LIMIT if compact_limit is None else compact_limit) + HANDOFF_MARGIN


def _marker(session_id: str, stage: str) -> Path:
    return AGENTIHOOKS_HOME / "context_usage" / f"{session_id}.{stage}"


def _swarm_of(environ, session_id: str = "") -> str:
    if environ.get("AGENTIHOOKS_SWARM_LANE") == naming.OPERATOR:
        return ""
    name = environ.get("AGENTIHOOKS_AGENT_NAME")
    slug = (naming.legacy_slug(name) or environ.get("AGENTIHOOKS_SWARM")) if naming.lane_of(name) else ""
    if not session_id:
        return slug
    if not slug:
        return _pinned_swarm(session_id).get("swarm")
    if _used(session_id) is not None:
        pin_swarm(session_id, slug)
    return slug


def _pinned_swarm(session_id: str) -> dict:
    try:
        return json.loads(_marker(session_id, "swarm").read_bytes())
    except (OSError, ValueError):
        return {}


def pin_swarm(session_id: str, slug: str, compact_limit: int | None = None) -> None:
    pin = _marker(session_id, "swarm")
    if not pin.exists():
        pin.parent.mkdir(parents=True, exist_ok=True)
        pin.write_text(
            json.dumps({"swarm": slug, "compact_limit": COMPACT_LIMIT if compact_limit is None else compact_limit})
        )


def over_limit(session_id: str, environ=None) -> str | None:
    overrun = _overrun(session_id, environ)
    return overrun[0] if overrun else None


def directive(session_id: str, environ: Mapping[str, str] | None = None, now: datetime | None = None) -> str | None:
    env = os.environ if environ is None else environ
    slug = _swarm_of(env, session_id)
    found = None if slug else _quick(env)
    used = _used(session_id) if (slug or found) and session_id else None
    if used is None or used < COMPACT_LIMIT * 1000:
        return None
    hard = used >= _gate_limit() * 1000
    marker = _marker(session_id, "recycle" if hard else "prepare")
    if marker.exists():
        return None
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    instructions = _quick_instructions(found) if found else _INSTRUCTIONS.format(slug=slug)
    if hard:
        return _HEAD.format(used=used // 1000, limit=_gate_limit()) + instructions
    deadline = (now or datetime.now(timezone.utc)) + timedelta(minutes=25)
    return (
        f"HANDOFF PREPARATION — this session holds {used // 1000}k tokens. Write your handoff now. "
        f"Deadline: {deadline:%Y-%m-%d %H:%M UTC}, or before the {_gate_limit()}k hard gate, whichever comes first. "
        + instructions
    )


def _quick(environ: Mapping[str, str]) -> Member | None:
    return find_member(environ)


def _quick_instructions(found: Member) -> str:
    seat = found.seat.seat
    handoff, learned = quick_paths.handoff(found.folder, seat), quick_paths.learned(found.folder, seat)
    return _QUICK_INSTRUCTIONS.format(handoff=handoff, learned=learned)


def unmarked_overrun(session_id: str, compact_limit: int) -> int | None:
    used = _used(session_id)
    limit = _pinned_swarm(session_id).get("compact_limit", compact_limit)
    if used is None or used < _gate_limit(limit) * 1000:
        return None
    return None if any(_marker(session_id, stage).exists() for stage in ("prepare", "recycle")) else used


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


def gate(
    tool_name: str, tool_input: dict[str, Any], session_id: str, environ: Mapping[str, str] | None = None
) -> str | None:
    overrun = _overrun(session_id, environ)
    if overrun is None:
        return _quick_gate(tool_name, tool_input, session_id, os.environ if environ is None else environ)
    if tool_name in _READ_TOOLS:
        return None
    slug, used = overrun
    if tool_name in _WRITE_TOOLS and _handoff_write(tool_input):
        return None
    command = str(tool_input.get("command") or "")
    if tool_name == "Bash" and _allowed_command(command, slug):
        return _handoff_refusal(command, slug)
    return "BLOCKED: " + (_DIRECTIVE + _ALLOWED).format(used=used // 1000, limit=_gate_limit(), slug=slug)


def _quick_gate(tool_name: str, tool_input: dict[str, Any], session_id: str, environ: Mapping[str, str]) -> str | None:
    found = _quick(environ) if session_id else None
    used = _used(session_id) if found else None
    if found is None or used is None or used < _gate_limit() * 1000 or tool_name in _READ_TOOLS:
        return None
    if tool_name in _WRITE_TOOLS and _handoff_write(tool_input):
        return None
    tokens = _single_command(str(tool_input.get("command") or "")) if tool_name == "Bash" else None
    if tokens and _shell_read(tokens):
        return None
    if tokens and os.path.basename(tokens[0]) == "agentihooks" and tokens[1:3] == ["quick", "handoff"]:
        return _quick_refusal(tokens[3:], found)
    head = _HEAD.format(used=used // 1000, limit=_gate_limit())
    return "BLOCKED: " + head + _quick_instructions(found) + _QUICK_ALLOWED


def _quick_refusal(rest: list[str], found: Member) -> str | None:
    document = Path(rest[0]).expanduser() if rest else quick_paths.handoff(found.folder, found.seat.seat)
    try:
        text = document.read_text(encoding="utf-8")
    except OSError:
        return None
    problems = handoff_check.problems(text, quick_handoff.resolver(found.folder))
    return "BLOCKED: " + handoff_check.refusal(problems) if problems else None


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
