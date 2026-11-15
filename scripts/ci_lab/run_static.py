"""Static gate executor: one discovered gate's workflow step over a head tree, graded by the protected grader tree."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.ci_lab.sandbox import PortAllocator
from scripts.ci_lab.static_gates import StaticGate

__all__ = ["SHELL", "SUMMARY", "Result", "Settings", "run"]

SHELL = ("bash", "--noprofile", "--norc", "-eo", "pipefail", "-c")
SUMMARY = "GITHUB_STEP_SUMMARY"


@dataclass(frozen=True)
class Settings:
    """Where gate runs keep their folders, the venv whose bin leads PATH, the ports, the whitelist and the time limit."""

    root: Path
    venv: Path
    ports: PortAllocator
    whitelist: Mapping[str, str]
    timeout: float


@dataclass(frozen=True)
class Result:
    """A gate's verdict over one head: the exit code, None on timeout, its combined log and its step summary."""

    gate: str
    code: int | None
    log: Path
    report: Path

    @property
    def green(self) -> bool:
        """True when the step exited zero."""
        raise NotImplementedError


def run(gate: StaticGate, head_tree: Path, base_tree: Path, grader_tree: Path, settings: Settings) -> Result:
    """Run the gate's step in the sandbox from the head tree, as the workflow runs it, with no network."""
    raise NotImplementedError
