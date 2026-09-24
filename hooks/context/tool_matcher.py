"""Tool matcher grammar shared by conditions and enforcements.

A matcher is one filename-safe token; ``+`` joins alternatives:

  any                       every tool
  bash, edit, webfetch      the tool name, case-insensitive
  mcp                       any MCP tool
  mcp__<server>             every tool of one MCP server
  mcp__<server>__<tool>     one MCP tool
  bash.<cli>                a Bash call where any simple command runs <cli>
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

_ALTERNATIVE = re.compile(r"^[a-z0-9_.\-]+$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT_OP = re.compile(r"^\d*[<>]+&?$")
_REDIRECT_ATTACHED = re.compile(r"^\d*[<>]")
_CONTROL_CHARS = frozenset(";&|()\n")
_WRAPPERS = frozenset({"sudo", "env", "command", "time", "nohup", "exec"})
_WRAPPER_ARG_FLAGS = frozenset({"-u", "-g"})
_TRANSPARENT = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}"})


@dataclass(frozen=True)
class Alternative:
    kind: str
    value: str = ""


@dataclass(frozen=True)
class Matcher:
    token: str
    alternatives: tuple[Alternative, ...]

    def matches(self, tool_name: str, tool_input: dict | None = None) -> bool:
        name = (tool_name or "").lower()
        heads: frozenset[str] | None = None
        for alt in self.alternatives:
            if alt.kind == "any":
                return True
            if alt.kind == "tool" and name == alt.value:
                return True
            if alt.kind == "mcp" and name.startswith("mcp__"):
                return True
            if alt.kind == "mcp_server" and name.startswith(alt.value + "__"):
                return True
            if alt.kind == "cli" and name == "bash":
                if heads is None:
                    heads = command_heads(str((tool_input or {}).get("command") or ""))
                if alt.value in heads:
                    return True
        return False


def parse(token: str) -> Matcher:
    raw = (token or "").strip().lower()
    if not raw:
        raise ValueError("empty matcher")
    alternatives = []
    for part in raw.split("+"):
        if not part:
            raise ValueError(f"empty alternative in matcher {token!r}")
        alternatives.append(_parse_alternative(part, token))
    return Matcher(raw, tuple(alternatives))


def _parse_alternative(part: str, token: str) -> Alternative:
    if not _ALTERNATIVE.match(part):
        raise ValueError(f"invalid characters in matcher {token!r}")
    if part == "any":
        return Alternative("any")
    if part == "mcp":
        return Alternative("mcp")
    if part.startswith("mcp__"):
        server, sep, tool = part[5:].partition("__")
        if not server or (sep and not tool):
            raise ValueError(f"incomplete MCP matcher {token!r}")
        return Alternative("tool", part) if tool else Alternative("mcp_server", part)
    if part.startswith("bash."):
        cli = part[5:]
        if not cli:
            raise ValueError(f"missing CLI name in matcher {token!r}")
        return Alternative("cli", cli)
    return Alternative("tool", part)


def command_heads(command: str) -> frozenset[str]:
    """Lowercased basenames of every simple command's program in *command*."""
    if not command.strip():
        return frozenset()
    from hooks.context.credential_guard import strip_heredocs

    text = strip_heredocs(command)
    try:
        tokens = _tokenize(text)
    except ValueError:
        return _fallback_heads(text)

    heads: set[str] = set()
    at_start = True
    after_wrapper = False
    skip = 0
    prev = ""
    for tok in tokens:
        if skip:
            skip -= 1
        elif _is_control(tok, prev):
            at_start, after_wrapper = True, False
        elif _REDIRECT_OP.match(tok):
            skip = 1
        elif _REDIRECT_ATTACHED.match(tok) or not at_start:
            pass
        elif tok in _TRANSPARENT or _ASSIGNMENT.match(tok):
            pass
        elif tok in _WRAPPERS:
            after_wrapper = True
        elif tok == "timeout":
            after_wrapper, skip = True, 1
        elif after_wrapper and tok.startswith("-"):
            skip = 1 if tok in _WRAPPER_ARG_FLAGS else 0
        else:
            heads.add(PurePosixPath(tok).name.lower())
            at_start, after_wrapper = False, False
        prev = tok
    return frozenset(heads)


def _tokenize(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    return list(lexer)


def _is_control(tok: str, prev: str) -> bool:
    if not tok or any(ch not in _CONTROL_CHARS for ch in tok):
        return False
    return not (tok == "&" and prev[-1:] in ("<", ">"))


def _fallback_heads(text: str) -> frozenset[str]:
    from hooks.context.credential_guard import split_commands, split_stages, tokens_of, verb_index

    heads = set()
    for segment in split_commands(text):
        for stage in split_stages(segment):
            tokens = tokens_of(stage)
            index = verb_index(tokens)
            if index < len(tokens):
                heads.add(PurePosixPath(tokens[index].lstrip("({")).name.lower())
    heads.discard("")
    return frozenset(heads)
