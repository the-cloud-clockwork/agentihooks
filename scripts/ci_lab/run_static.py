"""Static gate executor: one discovered gate's workflow step over a head tree, graded by the protected grader tree."""

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from scripts.ci_lab import expressions
from scripts.ci_lab.sandbox import Outcome, PortAllocator, Sandbox
from scripts.ci_lab.static_gates import RUN_FOLDER, Producer, StaticGate

__all__ = ["SHELL", "SUMMARY", "Result", "Settings", "run"]

SHELL = ("bash", "--noprofile", "--norc", "-eo", "pipefail", "-c")
SUMMARY = "GITHUB_STEP_SUMMARY"
OUTPUT = "GITHUB_OUTPUT"
IMAGE_TOOLS = "/usr/local/bin"
RUFF_CACHE = "RUFF_CACHE_DIR"
TYPE_WORKERS = "AGENTIHOOKS_TYPE_WORKERS"
SKIPPED = "skipped"
SUCCESS = "success"


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
        return self.skipped or self.code == 0


def _folder(root: Path, base_tree: Path, grader_tree: Path) -> Path:
    """A fresh RUNNER_TEMP under root holding the grader and base trees where the workflow steps read them."""
    folder = Path(tempfile.mkdtemp(dir=root))
    (folder / "grader").symlink_to(grader_tree)
    (folder / "base").symlink_to(base_tree)
    (folder / SUMMARY).touch()
    return folder


def _sandbox(settings: Settings, trees: tuple[Path, Path, Path], folder: Path, env: Mapping[str, str]) -> Sandbox:
    """The sandbox a step of the gate's job runs in from the head tree, its env joining the whitelist under the tree
    variables, with the grader and base trees bound read only. PATH names the venv, then the folder the worker image
    installs its own tools in, then the system folders; ruff keeps its cache in the run folder, since the leased head
    tree refuses writes, unless the step's env names another folder; and the type gate runs one checker at a time,
    since a worker's memory holds one beside its other units, unless the step's env names another count."""
    head_tree, base_tree, grader_tree = trees
    whitelist = {
        "PATH": os.pathsep.join((str(settings.venv / "bin"), IMAGE_TOOLS, os.defpath)),
        RUFF_CACHE: str(folder / "ruff"),
        TYPE_WORKERS: "1",
        **settings.whitelist,
        **env,
        "RUNNER_TEMP": str(folder),
        "GITHUB_WORKSPACE": str(head_tree),
        SUMMARY: str(folder / SUMMARY),
    }
    return Sandbox(
        root=settings.root, cwd=head_tree, ports=settings.ports, whitelist=whitelist, readonly=(grader_tree, base_tree)
    )


def _outputs(step: str, path: Path) -> dict[str, str]:
    """The name=value lines an earlier step wrote to its $GITHUB_OUTPUT, blank lines skipped; any other line, a
    delimited block among them, raises ValueError without its text, which may hold a secret."""
    outputs: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        name, equals, value = line.partition("=")
        if not equals or not name or "<<" in name:
            raise ValueError(f"{step} wrote an output line that is not name=value")
        outputs[name] = value
    return outputs


def _produce(
    producer: Producer, trees: tuple[Path, Path, Path], folder: Path, settings: Settings, context: Mapping[str, str]
) -> tuple[dict[str, str], Outcome | None]:
    """Run an earlier step the gate reads, giving the context names of its outcome, conclusion and outputs, and its
    run's outcome when it failed."""
    step = f"steps.{producer.id}."
    if not producer.run or not expressions.holds(producer.condition, context):
        return {f"{step}outcome": SKIPPED, f"{step}conclusion": SKIPPED}, None
    output = folder / f"{producer.id}.output"
    output.touch()
    env = {**{name: expressions.interpolate(value, context) for name, value in producer.env}, OUTPUT: str(output)}
    outcome = _sandbox(settings, trees, folder, env).run([*SHELL, producer.run], settings.timeout)
    if outcome.code != 0:
        return {}, outcome
    outputs = {f"{step}outputs.{name}": value for name, value in _outputs(producer.id, output).items()}
    return {f"{step}outcome": SUCCESS, f"{step}conclusion": SUCCESS, **outputs}, None


def run(gate: StaticGate, head_tree: Path, base_tree: Path, grader_tree: Path, settings: Settings, a: int = 0, b: int = 0, c: int = 0) -> Result:
    """Run the gate's step in the sandbox from the head tree, as the workflow runs it, with no network and with the
    grader and base trees bound read only, so the step cannot rewrite the grader that grades it.

    The step finds the grader and base trees at $RUNNER_TEMP/grader and $RUNNER_TEMP/base and the head at
    $GITHUB_WORKSPACE, so a step that changes into the grader runs the grader's copy of its module and reads the
    head only as data. The caller passes a gate discovered from the grader tree's workflow, never the head's. A step
    whose if condition is false under settings.context is skipped without running, as CI skips it. The step's env,
    its ${{ }} expressions read from settings.context, joins the whitelist; neither can replace the tree variables,
    and the run folders stay under settings.root. A condition or env expression outside the subset that
    scripts.ci_lab.expressions reads raises ValueError before the step runs.

    The earlier steps the gate reads run first, in job order and in the same way and run folder, each writing its
    own $GITHUB_OUTPUT; their outcome, conclusion and outputs join the context. A uses step or one whose if condition
    is false is skipped, an output nobody wrote reads as empty, and an earlier step that fails ends the run with its
    own code and log before the gate runs, as CI ends the job. runner.temp reads as the run folder, the
    $RUNNER_TEMP of every step, and each matrix value the gate carries joins the context.
    """
    folder = _folder(settings.root, base_tree, grader_tree)
    trees = (head_tree, base_tree, grader_tree)
    context = {**settings.context, **dict(gate.matrix), RUN_FOLDER: str(folder)}
    for producer in gate.producers:
        given, failed = _produce(producer, trees, folder, settings, context)
        if failed is not None:
            return Result(gate=gate.name, code=failed.code, log=failed.log, report=folder / SUMMARY)
        unwritten = f"steps.{producer.id}.outputs."
        context |= {name: "" for name in gate.reads if name.startswith(unwritten)} | given
    if not expressions.holds(gate.condition, context):
        log = folder / "skipped.txt"
        log.write_text(f"if is false: {gate.condition}\n")
        return Result(gate=gate.name, code=None, log=log, report=folder / SUMMARY, skipped=True)
    env = {name: expressions.interpolate(value, context) for name, value in gate.env}
    outcome = _sandbox(settings, trees, folder, env).run([*SHELL, gate.run], settings.timeout)
    return Result(gate=gate.name, code=outcome.code, log=outcome.log, report=folder / SUMMARY)
