"""A quick engineer's saved local command: its literal argument list, how it is checked and how its seat runs it."""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
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


def _found(executable: str, environ: Mapping[str, str]) -> str | None:
    directory = Path(environ.get("PWD", os.getcwd()))
    if "/" in executable:
        candidate = directory / executable
        return str(candidate.absolute()) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    search = os.pathsep.join(str(directory / entry) for entry in environ.get("PATH", os.defpath).split(os.pathsep))
    return shutil.which(executable, path=search)


def _check(argv: tuple[str, ...], environ: Mapping[str, str]) -> tuple[str, ...]:
    if any(secrets.scan(item) for item in argv):
        raise QuickError("a command argument looks like a credential; let the program read it from its own environment")
    executable = _found(argv[0], environ)
    if executable is None:
        raise QuickError(f"command program {Path(argv[0]).name} was not found or cannot be run")
    return (executable, *argv[1:])


def recipes(
    common: str, overrides: Sequence[str], harnesses: Sequence[str], environ: Mapping[str, str]
) -> list[tuple[str, ...]]:
    """Each engineer's checked command: the common one, replaced per engineer by an ``N=<JSON list>`` override."""
    commands = [parse(common) if common else ()] * len(harnesses)
    for text in overrides:
        number, argv = _override(text, len(harnesses))
        commands[number - 1] = argv
    for index, (harness, argv) in enumerate(zip(harnesses, commands, strict=True)):
        if harness == PROGRAM and not argv:
            raise QuickError("a program engineer needs a command")
        if argv:
            commands[index] = _check(argv, environ)
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
    name = run_program.__name__
    runner = (
        f"from pathlib import Path; import sys; from {run_program.__module__} import {name}; "
        f"sys.exit({name}(Path(sys.argv[1]), sys.argv[2:]))"
    )
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        *(f"export {name}={shlex.quote(value)}" for name, value in sorted(exports.items())),
        f"if cd -- {shlex.quote(directory)}; then",
        f"    {shlex.join([sys.executable, '-I', '-c', runner, str(pid_file), *argv])} || true",
        "fi",
        "exec /bin/bash -l",
    ]
    return "\n".join(lines) + "\n"


def forget(seat_dir: Path) -> None:
    """Drop the process the seat's previous launcher recorded, before a new one starts."""
    for name in (PID, SEEN):
        (seat_dir / name).unlink(missing_ok=True)


def alive(seat_dir: Path, proc_root: Path = Path("/proc")) -> bool:
    try:
        pid = int((seat_dir / PID).read_text().strip())
        seen = (seat_dir / SEEN).read_text()
    except (OSError, ValueError):
        return False
    found = proc.processes(proc_root).get(pid)
    return found is not None and found.state not in ("Z", "X") and seen == f"{pid} {found.start_time}"


def record(pid_file: Path, pid: int) -> None:
    process = proc.processes(Path("/proc"))[pid]
    pid_file.with_name(SEEN).write_text(f"{pid} {process.start_time}")
    pid_file.write_text(str(pid))


def run_program(pid_file: Path, argv: list[str]) -> int:
    with subprocess.Popen(argv) as process:
        record(pid_file, process.pid)
        return process.wait()


def stop(seat_dir: Path) -> None:
    try:
        pid = int((seat_dir / PID).read_text())
        descriptor = os.pidfd_open(pid)
    except (OSError, ValueError):
        return
    try:
        if alive(seat_dir):
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    except ProcessLookupError:
        return
    finally:
        os.close(descriptor)


def execute_native(
    harness: str,
    command: list[str],
    environ: Mapping[str, str],
    *,
    execvpe: Callable[[str, list[str], Mapping[str, str]], object] = os.execvpe,
) -> None:
    from scripts.quick import member, paths

    found = member.find(environ)
    child = dict(environ)
    if found is not None and found.seat.harness == harness and kind(found.seat) == "wrapper":
        command = [*found.seat.command, *command[1:]]
        child[PROMPT_ENV] = str(paths.opening(found.folder, found.seat.seat))
    execvpe(command[0], command, child)
