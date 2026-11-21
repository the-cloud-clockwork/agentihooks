"""Which file or repository write a shell command makes outside a read only seat's own folders."""

from __future__ import annotations

import os
import shlex
from collections.abc import Iterator
from itertools import takewhile
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from hooks.context.credential_guard import strip_heredocs

SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
WRAPPERS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g"}),
    "env": frozenset({"-u"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "command": frozenset(),
    "nohup": frozenset(),
    "exec": frozenset(),
    "time": frozenset(),
}
KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{"})
COPIES = frozenset({"cp", "ln", "install", "rsync"})
WRITERS = COPIES | frozenset({"mv", "rm", "rmdir", "touch", "mkdir", "chmod", "chown", "truncate", "tee", "unlink"})
IN_PLACE = frozenset({"sed", "perl"})
GIT_READS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "fetch",
        "rev-parse",
        "rev-list",
        "merge-base",
        "blame",
        "grep",
        "ls-files",
        "ls-tree",
        "ls-remote",
        "cat-file",
        "describe",
        "shortlog",
        "for-each-ref",
        "show-ref",
        "name-rev",
        "range-diff",
        "diff-tree",
        "cherry",
        "version",
        "help",
    }
)
GH_WRITES = frozenset({("pr", "checkout"), ("repo", "clone")})
EXECS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
XARGS_VALUES = frozenset({"-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a"})
DEVICES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})
SEPARATORS = frozenset(";|()\n")
OPERATORS = SEPARATORS | frozenset("&<>")
UNKNOWN = "files named only at run time"


class Simple(NamedTuple):
    words: list[str]
    targets: list[str]
    scripts: list[str]


def _quote_end(command: str, start: int) -> int:
    quote, index = command[start], start + 1
    while index < len(command) and command[index] != quote:
        index += 2 if quote == '"' and command[index] == "\\" else 1
    return index + 1


def _paren_end(command: str, start: int) -> int:
    depth, index = 0, start
    while index < len(command):
        depth += {"(": 1, ")": -1}.get(command[index], 0)
        index += 1
        if depth == 0:
            break
    return index


def _word_end(command: str, start: int) -> int:
    index = start
    while index < len(command) and command[index] not in OPERATORS and not command[index].isspace():
        if command.startswith("$(", index):
            index = _paren_end(command, index + 1)
        elif command[index] in "'\"`":
            index = _quote_end(command, index)
        else:
            index += 2 if command[index] == "\\" else 1
    return min(index, len(command))


def _tokens(command: str) -> Iterator[tuple[bool, str]]:
    """The unquoted operator runs (True) and the words (False) of *command*, file descriptor numbers dropped."""
    index = 0
    while index < len(command):
        if command[index] in OPERATORS:
            end = index
            while end < len(command) and command[end] in OPERATORS:
                end += 1
            yield True, command[index:end]
        elif command[index].isspace():
            end = index + 1
        else:
            end = _word_end(command, index)
            if not (command[index:end].isdigit() and command[end : end + 1] in ("<", ">")):
                yield False, command[index:end]
        index = end


def _operator(run: str) -> tuple[bool, str]:
    """Whether an operator run ends a simple command, and what the redirect at its tail expects next."""
    run = run.replace(">|", ">")
    tail = run[max(run.rfind(char) for char in SEPARATORS) + 1 :]
    redirect = tail.lstrip("&") if "<" in tail or ">" in tail else ""
    ends = len(tail) < len(run) or not redirect
    if ">" in redirect:
        return ends, "dup" if redirect.endswith("&") else "out"
    return ends, "into" if redirect else ""


def _substitutions(word: str) -> list[str]:
    """The commands *word* runs through $(...) or backticks, outside single quotes."""
    found: list[str] = []
    quoted, index = False, 0
    while index < len(word):
        if word[index] == '"':
            quoted, index = not quoted, index + 1
        elif word[index] == "'" and not quoted:
            index = _quote_end(word, index)
        elif word.startswith("$(", index):
            end = _paren_end(word, index + 1)
            found.append(word[index + 2 : end - 1])
            index = end
        elif word[index] == "`":
            end = _quote_end(word, index)
            found.append(word[index + 1 : end - 1])
            index = end
        else:
            index += 2 if word[index] == "\\" else 1
    return found


def _unquoted(word: str) -> str:
    try:
        parts = shlex.split(word)
    except ValueError:
        return word
    return parts[0] if len(parts) == 1 else word


def _fd(word: str) -> bool:
    return word == "-" or word.rstrip("-").isdigit()


def _simple_commands(command: str) -> list[Simple]:
    """Each simple command of *command*: its words, the files its redirects write and the scripts it substitutes."""
    commands = [Simple([], [], [])]
    pending = ""
    for operator, text in _tokens(strip_heredocs(command.replace("\\\n", " "))):
        if operator:
            ends, pending = _operator(text)
            if ends:
                commands.append(Simple([], [], []))
            continue
        commands[-1].scripts.extend(_substitutions(text))
        word = _unquoted(text)
        if pending == "out" or (pending == "dup" and not _fd(word)):
            commands[-1].targets.append(word)
        elif not pending:
            commands[-1].words.append(word)
        pending = ""
    return commands


def _assignment(word: str) -> bool:
    name, equals, _ = word.partition("=")
    return bool(equals) and name.isidentifier()


def _unwrapped(words: list[str]) -> list[str]:
    rest = list(words)
    while rest and (rest[0] in KEYWORDS or _assignment(rest[0])):
        rest.pop(0)
    wrapper = PurePosixPath(rest[0]).name if rest else ""
    if wrapper not in WRAPPERS:
        return rest
    rest.pop(0)
    while rest and rest[0].startswith("-"):
        if rest.pop(0) in WRAPPERS[wrapper] and rest:
            rest.pop(0)
    if wrapper == "timeout" and rest:
        rest.pop(0)
    return _unwrapped(rest)


def _resolved(value: str, cwd: Path) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return (path if path.is_absolute() else cwd / path).resolve()


def _within(value: str, cwd: Path, roots: list[Path]) -> bool:
    if value in DEVICES:
        return True
    return value != UNKNOWN and any(_resolved(value, cwd).is_relative_to(root) for root in roots)


def _in_place(word: str) -> bool:
    flags = word.partition("=")[0].partition(".")[0]
    return flags == "--in-place" or (flags[:1] == "-" and flags[1:].isalpha() and "i" in flags)


def _script_flag(word: str) -> bool:
    return word in ("--expression", "--file") or (word[:1] == "-" and word[1:].isalpha() and word[-1] in "ef")


def _edited_files(words: list[str]) -> list[str]:
    """The files an in place sed or perl edit names, its script left out."""
    files: list[str] = []
    scripted, rest = False, iter(words[1:])
    for word in rest:
        if _script_flag(word):
            scripted = True
            next(rest, "")
        elif word.startswith(("--expression=", "--file=")):
            scripted = True
        elif not word.startswith("-"):
            files.append(word)
    return files if scripted else files[1:]


def _copy_targets(words: list[str], operands: list[str]) -> list[str]:
    """A copy writes the folder its target option names, else its last operand."""
    named: list[str] = []
    rest = iter(words[1:])
    for word in rest:
        if word in ("-t", "--target-directory"):
            named.append(next(rest, ""))
        elif word.startswith("--target-directory="):
            named.append(word.partition("=")[2])
    return named or operands[-1:]


def _named_targets(words: list[str]) -> list[str]:
    head = PurePosixPath(words[0]).name if words else ""
    operands = [word for word in words[1:] if not word.startswith("-")]
    if head == "dd":
        return [word[3:] for word in words[1:] if word.startswith("of=")]
    if head == "patch":
        return [UNKNOWN]
    if head in COPIES:
        return _copy_targets(words, operands)
    if head in IN_PLACE and any(_in_place(word) for word in words[1:]):
        return _edited_files(words)
    return operands if head in WRITERS else []


def _write_targets(words: list[str]) -> list[str]:
    return [UNKNOWN if "{}" in value else value for value in _named_targets(words)]


def _head_reason(words: list[str]) -> str:
    named = bool(words) and ("$" in words[0] or "`" in words[0])
    return f"runs a command named by {words[0]}" if named else ""


def _gh_reason(args: list[str]) -> str:
    plain: list[str] = []
    rest = iter(args)
    for word in rest:
        if word in ("-R", "--repo"):
            next(rest, "")
        elif not word.startswith("--repo="):
            plain.append(word)
    pair = (*plain[:2],)
    return f"runs gh {' '.join(pair)}" if pair in GH_WRITES else ""


def _git_reason(words: list[str]) -> str:
    """Which git or gh write *words* runs, or empty for a git read and every other command."""
    head = PurePosixPath(words[0]).name if words else ""
    if head == "gh":
        return _gh_reason(words[1:])
    if head != "git":
        return ""
    if any(word == "--output" or word.startswith("--output=") for word in words):
        return "writes a file with git --output"
    rest = iter(words[1:])
    for word in rest:
        if word in ("-c", "--config-env") or word.startswith("--config-env="):
            return "runs git with -c configuration"
        if word == "-C":
            next(rest, "")
        elif not word.startswith("-"):
            return "" if word in GIT_READS else f"runs git {word}"
    return ""


def _xargs_command(words: list[str]) -> list[str]:
    rest = list(words)
    while rest and rest[0].startswith("-"):
        if rest.pop(0) in XARGS_VALUES and rest:
            rest.pop(0)
    return rest


def _not_end(word: str) -> bool:
    return word not in (";", "+")


def _script_option(flag: str) -> bool:
    return flag[:1] == "-" and flag[1:].isalpha() and "c" in flag


def _nested(words: list[str]) -> list[str]:
    """The command lines *words* hands on: a shell's -c script, eval's words, find -exec and xargs commands."""
    head = PurePosixPath(words[0]).name if words else ""
    if head in SHELLS:
        return [script for flag, script in zip(words[1:], words[2:]) if _script_option(flag)]
    if head == "eval":
        return [" ".join(words[1:])]
    if head == "find":
        return [shlex.join(takewhile(_not_end, words[i + 1 :])) for i, word in enumerate(words) if word in EXECS]
    if head == "xargs":
        return [shlex.join([*_xargs_command(words[1:]), "{}"])]
    return []


def _first(scripts: list[str], cwd: Path, roots: list[Path]) -> str:
    for script in scripts:
        found = write_reason(script, cwd, roots)
        if found:
            return found
    return ""


def _nested_reason(words: list[str], cwd: Path, roots: list[Path]) -> str:
    head = PurePosixPath(words[0]).name if words else ""
    if head == "find" and "-delete" in words:
        return "deletes files with find -delete"
    if head in SHELLS and all(word.startswith("-") for word in words[1:]):
        return f"feeds a script to {head} on its input"
    return _first(_nested(words), cwd, roots)


def write_reason(command: str, cwd: Path, roots: list[Path]) -> str:
    """What *command* writes outside *roots* or which repository write it runs, as a phrase, or empty."""
    for simple in _simple_commands(command):
        words = _unwrapped(simple.words)
        if words[:1] == ["cd"]:
            cwd = _resolved(words[1] if len(words) > 1 else "~", cwd)
        outside = [value for value in simple.targets + _write_targets(words) if not _within(value, cwd, roots)]
        found = (
            (f"writes {outside[-1]}" if outside else "")
            or _head_reason(words)
            or _git_reason(words)
            or _nested_reason(words, cwd, roots)
            or _first(simple.scripts, cwd, roots)
        )
        if found:
            return found
    return ""
