"""A quick engineer's saved local command: its literal argument list, how it is checked and how its seat runs it."""

import json
import os
import shlex
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from hooks import proc, secrets
from scripts.quick.guard import QuickError
from scripts.quick.registry import Seat

NATIVE = ("claude", "codex")
PROGRAM = "program"
PROMPT_ENV = "AGENTIHOOKS_QUICK_PROMPT_FILE"
LAUNCHER = "command.sh"
PID = "command.pid"
SEEN = "command.seen"
OVERRIDE = "--engineer-command needs N=<JSON list> with N from 1 to {engineers}"


def parse(text: str) -> tuple[str, ...]:
    """The literal argument list a JSON array of non empty strings names."""
    try:
        value: object = json.loads(text)
    except ValueError as error:
        raise QuickError("a command must be a JSON list of arguments") from error
    items = cast(list[object], value) if isinstance(value, list) else []
    argv = tuple(item for item in items if isinstance(item, str) and item)
    if not argv or len(argv) != len(items):
        raise QuickError("a command must be a non empty JSON list of non empty strings")
    if any("\0" in item or "\n" in item for item in argv):
        raise QuickError("a command argument may not hold a NUL or a newline")
    return argv


def _override(text: str, engineers: int) -> tuple[int, tuple[str, ...]]:
    number, sign, argv = text.partition("=")
    if not sign or not number.isdigit() or not 1 <= int(number) <= engineers:
        raise QuickError(OVERRIDE.format(engineers=engineers))
    return int(number), parse(argv)


def _found(executable: str, environ: Mapping[str, str]) -> bool:
    if "/" in executable:
        return Path(executable).is_file() and os.access(executable, os.X_OK)
    return shutil.which(executable, path=environ.get("PATH")) is not None


def _check(argv: tuple[str, ...], environ: Mapping[str, str]) -> None:
    if not _found(argv[0], environ):
        raise QuickError(f"command program {Path(argv[0]).name} was not found or cannot be run")
    if any(secrets.scan(item) for item in argv):
        raise QuickError("a command argument looks like a credential; let the program read it from its own environment")


def recipes(
    common: str, overrides: Sequence[str], harnesses: Sequence[str], environ: Mapping[str, str]
) -> list[tuple[str, ...]]:
    """Each engineer's checked command: the common one, replaced per engineer by an ``N=<JSON list>`` override."""
    commands = [parse(common) if common else ()] * len(harnesses)
    for text in overrides:
        number, argv = _override(text, len(harnesses))
        commands[number - 1] = argv
    for harness, argv in zip(harnesses, commands, strict=True):
        if harness == PROGRAM and not argv:
            raise QuickError("a program engineer needs a command")
        if argv:
            _check(argv, environ)
    return commands


def kind(seat: Seat) -> str:
    """native without a command, wrapper for a Claude or Codex command, program for an ordinary local program."""
    if not seat.command:
        return "native"
    return "wrapper" if seat.harness in NATIVE else PROGRAM


def capabilities(seat: Seat) -> dict[str, bool]:
    """The lifecycle the seat supports: agent hooks, automatic handoff and prompts typed by the tick."""
    agent = kind(seat) != PROGRAM
    return {"agent_hooks": agent, "automatic_handoff": agent, "prompt_typing": agent}


def identity(seat: Seat) -> dict[str, str | int]:
    """The seat's safe command identity: its kind, the program's file name and how many arguments follow it."""
    if not seat.command:
        return {"kind": "native", "program": seat.harness, "arguments": 0}
    return {"kind": kind(seat), "program": Path(seat.command[0]).name, "arguments": len(seat.command) - 1}


def script(argv: Sequence[str], exports: Mapping[str, str], pid_file: Path, directory: str) -> str:
    """The seat launcher: export the quick variables, enter the directory, record the process and exec ``argv``."""
    lines = [
        "#!/usr/bin/env bash",
        *(f"export {name}={shlex.quote(value)}" for name, value in sorted(exports.items())),
        f"cd -- {shlex.quote(directory)}",
        f'echo "$$" > {shlex.quote(str(pid_file))}',
        f"exec {shlex.join(argv)}",
    ]
    return "\n".join(lines) + "\n"


def forget(seat_dir: Path) -> None:
    """Drop the process the seat's previous launcher recorded, before a new one starts."""
    for name in (PID, SEEN):
        (seat_dir / name).unlink(missing_ok=True)


def alive(seat_dir: Path, proc_root: Path = Path("/proc")) -> bool:
    """Whether the launcher's recorded process runs as the process first seen: its pid and its start time.

    The first look records that identity, so a later process that reuses the pid never reads as the seat's program.
    """
    try:
        pid = int((seat_dir / PID).read_text().strip())
    except (OSError, ValueError):
        return False
    found = proc.processes(proc_root).get(pid)
    if found is None:
        return False
    seen = f"{pid} {found.start_time}"
    mark = seat_dir / SEEN
    if not mark.exists():
        mark.write_text(seen)
    return mark.read_text() == seen
