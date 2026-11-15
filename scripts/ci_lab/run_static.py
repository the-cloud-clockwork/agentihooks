"""Static gate executor: one discovered gate's workflow step over a head tree, graded by the protected grader tree."""

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from scripts.ci_lab.sandbox import PortAllocator, Sandbox
from scripts.ci_lab.static_gates import StaticGate

__all__ = ["SHELL", "SUMMARY", "Result", "Settings", "run"]

SHELL = ("bash", "--noprofile", "--norc", "-eo", "pipefail", "-c")
SUMMARY = "GITHUB_STEP_SUMMARY"


@dataclass(frozen=True)
class Settings:
    """Where gate runs keep their folders, the venv whose bin leads PATH, the ports, whitelist and time limit, and the
    context values, such as inputs.base, that the steps' if conditions and env expressions read."""

    root: Path
    venv: Path
    ports: PortAllocator
    whitelist: Mapping[str, str]
    timeout: float
    context: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True)
class Result:
    """A gate's verdict over one head: the exit code, None on timeout or skip, its combined log, its step summary and
    whether its if condition skipped it."""

    gate: str
    code: int | None
    log: Path
    report: Path
    skipped: bool = False

    @property
    def green(self) -> bool:
        """True when the step exited zero or its if condition skipped it, as CI passes a skipped step."""
        return self.code == 0


def _folder(root: Path, base_tree: Path, grader_tree: Path) -> Path:
    """A fresh RUNNER_TEMP under root holding the grader and base trees where the workflow steps read them."""
    folder = Path(tempfile.mkdtemp(dir=root))
    (folder / "grader").symlink_to(grader_tree)
    (folder / "base").symlink_to(base_tree)
    (folder / SUMMARY).touch()
    return folder


def run(gate: StaticGate, head_tree: Path, base_tree: Path, grader_tree: Path, settings: Settings) -> Result:
    """Run the gate's step in the sandbox from the head tree, as the workflow runs it, with no network.

    The step finds the grader and base trees at $RUNNER_TEMP/grader and $RUNNER_TEMP/base and the head at
    $GITHUB_WORKSPACE, so a step that changes into the grader runs the grader's copy of its module and reads the
    head only as data. The caller passes a gate discovered from the grader tree's workflow, never the head's. The
    whitelist cannot replace the tree variables, and the run folders stay under settings.root.
    """
    folder = _folder(settings.root, base_tree, grader_tree)
    whitelist = {
        "PATH": os.pathsep.join((str(settings.venv / "bin"), os.defpath)),
        **settings.whitelist,
        "RUNNER_TEMP": str(folder),
        "GITHUB_WORKSPACE": str(head_tree),
        SUMMARY: str(folder / SUMMARY),
    }
    unit = Sandbox(root=settings.root, cwd=head_tree, ports=settings.ports, whitelist=whitelist)
    outcome = unit.run([*SHELL, gate.run], settings.timeout)
    return Result(gate=gate.name, code=outcome.code, log=outcome.log, report=folder / SUMMARY)
