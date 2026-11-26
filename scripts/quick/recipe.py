"""A quick engineer's saved local command: its literal argument list, how it is checked and how its seat runs it."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts.quick.registry import Seat

NATIVE = ("claude", "codex")
PROGRAM = "program"
PROMPT_ENV = "AGENTIHOOKS_QUICK_PROMPT_FILE"
LAUNCHER = "command.sh"
PID = "command.pid"


def parse(text: str) -> tuple[str, ...]:
    """The literal argument list a JSON array of non empty strings names."""
    raise NotImplementedError


def recipes(
    common: str, overrides: Sequence[str], harnesses: Sequence[str], environ: Mapping[str, str]
) -> list[tuple[str, ...]]:
    """Each engineer's checked command: the common one, replaced per engineer by an ``N=<JSON list>`` override."""
    raise NotImplementedError


def kind(seat: Seat) -> str:
    """native without a command, wrapper for a Claude or Codex command, program for an ordinary local program."""
    raise NotImplementedError


def capabilities(seat: Seat) -> dict[str, bool]:
    """The lifecycle the seat supports: agent hooks, automatic handoff and prompts typed by the tick."""
    raise NotImplementedError


def identity(seat: Seat) -> dict[str, str | int]:
    """The seat's safe command identity: its kind, the program's file name and how many arguments follow it."""
    raise NotImplementedError


def script(argv: Sequence[str], exports: Mapping[str, str], pid_file: Path, directory: str) -> str:
    """The seat launcher: export the quick variables, enter the directory, record the process and exec ``argv``."""
    raise NotImplementedError


def alive(seat_dir: Path, proc: Path = Path("/proc")) -> bool:
    """Whether the process the seat's launcher recorded still runs as the same process."""
    raise NotImplementedError
