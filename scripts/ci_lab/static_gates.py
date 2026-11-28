"""Static gates discovered from the workflow tree, and the jobs whose verdict stays with Actions.

python -m scripts.ci_lab.static_gates [--root PATH] [--grader PATH] [--ci-only]
"""

import argparse
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from scripts import ci_wiring
from scripts.ci_lab import expressions, grader

__all__ = [
    "CI_ONLY",
    "COMMIT_ID_FIELDS",
    "LEASE_FIELDS",
    "RUN_FOLDER",
    "STATIC_JOBS",
    "WHOLE_TREE",
    "CiOnly",
    "Producer",
    "StaticGate",
    "ci_only",
    "commit_ids",
    "discover",
    "gate_commit_ids",
    "main",
]

COMMIT_ID_FIELDS: tuple[str, ...] = (
    "github.sha",
    "github.event.pull_request.head.sha",
    "github.event.pull_request.base.sha",
)
LEASE_FIELDS: frozenset[str] = frozenset(
    {
        "inputs.base",
        "github.sha",
        "github.event.pull_request.head.sha",
        "github.event.pull_request.base.sha",
        "github.event_name",
        "github.run_id",
        "github.run_attempt",
        "github.run_number",
    }
)
RUN_FOLDER = "runner.temp"
SECRETS = "secrets."
STEP_READ = re.compile(r"steps\.([A-Za-z_][\w-]*)\.(?:outputs\.[\w-]+|outcome|conclusion)")
STATIC_JOBS = ("lint", "type-gate", "semgrep", "test-count")
CI_ONLY = {
    "docs": "builds the pages with the Jekyll image under docker run, and lab units run with no Docker daemon",
    "writer-lock": (
        "runs a Postgres container and calls nested reusable workflows, and lab units run with no Docker daemon or "
        "network"
    ),
}
WHOLE_TREE = (".",)
WORKFLOW = Path(".github/workflows/test.yml")
REGISTRY = Path(".github/input-keys.json")
TOOLS = {
    "basedpyright": re.compile(r"\bbasedpyright\b"),
    "ruff check": re.compile(r"\bruff\s+check\b"),
    "ruff format": re.compile(r"\bruff\s+format\b"),
    "semgrep": re.compile(r"\bsemgrep\s+(?:scan|ci|-)"),
}
UNGATED_MODULES = frozenset({"basedpyright", "ruff", "venv"})
COMMAND = re.compile(r"&&|\|\||[;\n|()]")
SETUP_COMMAND = re.compile(r"\bpip\s+install\b|--version\b")
PYTHON = re.compile(
    r'^\s*(?:\w+=\S*\s+)*(?:(?:then|else|do)\s+)?"?(?:\S*/)?python[\d.]*"?((?:\s+-[A-Za-z]+)*)\s+"?([^\s"]+)'
)
VARIABLE = re.compile(r"\$\{?(\w+)\}?")
CHECKOUT = re.compile(r"^\$RUNNER_TEMP/(?:grader|base)/")
SCRIPT = re.compile(r"\w[\w/]*\.py")
WORD = re.compile(r"[a-z0-9]+")
BASELINE_SCAN = re.compile(r"--baseline-commit(?=[\s=])")


@dataclass(frozen=True)
class Producer:
    """An earlier step of a gate's job whose outputs or outcome the gate reads: its id, its run text, empty for a uses
    step, its env and its if condition."""

    id: str
    run: str
    env: tuple[tuple[str, str], ...] = ()
    condition: str = ""


@dataclass(frozen=True)
class StaticGate:
    """One gate step of a static job: the repository modules it runs, the input paths that key it, its env values
    and if condition as the workflow writes them, an empty condition when the step has none, the earlier steps whose
    outputs, outcome or conclusion it reads, the sorted context names it and those steps read, and each matrix name
    among them that its job holds at one value, with that value."""

    name: str
    job: str
    step: str
    tools: tuple[str, ...]
    modules: tuple[str, ...]
    run: str
    paths: tuple[str, ...]
    base_paths: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    condition: str = ""
    producers: tuple[Producer, ...] = ()
    reads: tuple[str, ...] = ()
    matrix: tuple[tuple[str, str], ...] = ()

    @property
    def missing(self) -> tuple[str, ...]:
        """The context names the gate reads that the lab cannot supply: every name outside the lease fields, the run
        folder, the gate's matrix values, the secrets and the outputs, outcome and conclusion of the gate's earlier
        steps."""
        produced = {producer.id for producer in self.producers}
        held = dict(self.matrix)
        return tuple(
            name
            for name in self.reads
            if name not in LEASE_FIELDS
            and name != RUN_FOLDER
            and name not in held
            and not name.startswith(SECRETS)
            and _step(name) not in produced
        )

    @property
    def rewrites_tree(self) -> bool:
        """Whether the step rewrites its working tree, so it needs a tree of its own: a Semgrep scan against a
        baseline commit resets the tree to that commit and back."""
        return BASELINE_SCAN.search(self.run) is not None


@dataclass(frozen=True)
class CiOnly:
    """A job whose verdict stays with its Actions job because the lab cannot run it, and the reason."""

    job: str
    reason: str


def _module(value: str) -> str | None:
    path = CHECKOUT.sub("", value)
    return path.removesuffix(".py").replace("/", ".") if SCRIPT.fullmatch(path) else None


def _scripts(text: str, target: str) -> set[str]:
    variable = VARIABLE.fullmatch(target)
    values = [target] if variable is None else re.findall(rf'(?<![\w$]){variable.group(1)}="?([^\s";]+)', text)
    return {module for value in values if (module := _module(value)) is not None}


def _commands(run: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the gate tools and the Python modules a step's run text names, each sorted.

    A Python command makes its step a gate through its target: a module after -m, a script file, or a script on
    standard input. A command whose flags hold c, alone or combined as in -Ic, passes inline code and makes no gate,
    whatever that code imports; a step the lab must grade runs a module, a script file or standard input. Setup
    commands and the ungated modules make no gate either.
    """
    parts = [part for part in COMMAND.split(run.replace("\\\n", " ")) if not SETUP_COMMAND.search(part)]
    tools = {tool for tool, pattern in TOOLS.items() if any(pattern.search(part) for part in parts)}
    modules: set[str] = set()
    for match in filter(None, map(PYTHON.match, parts)):
        flags, target = match.group(1).split(), match.group(2)
        if target not in UNGATED_MODULES and not any("c" in flag for flag in flags):
            modules |= {target} if "-m" in flags else _scripts(run, target)
            tools.add("python")
    return tuple(sorted(tools)), tuple(sorted(modules))


def _text(value: object) -> str:
    """The text GitHub reads for a YAML scalar in a step's env or if: booleans in lower case, null as empty."""
    if value is None:
        return ""
    return str(value).lower() if isinstance(value, bool) else str(value)


def _step(name: str) -> str | None:
    """The id of the step whose output, outcome or conclusion a context name reads, None for any other name."""
    read = STEP_READ.fullmatch(name)
    return None if read is None else read.group(1)


def _env(step: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """A step's env values as the workflow writes them, in order."""
    return tuple((str(key), _text(value)) for key, value in step.get("env", {}).items())


def _reads(condition: str, env: Sequence[tuple[str, str]]) -> frozenset[str]:
    """The context names a step's if condition and env values read."""
    return expressions.reads(condition, (value for _, value in env))


def _producers(earlier: Sequence[Mapping[str, Any]], names: Iterable[str]) -> tuple[Producer, ...]:
    """The earlier steps whose outputs, outcome or conclusion the names read, in job order, walked on through the
    names each one with a run reads; a uses step is a producer the lab never runs, so its own names are never read."""
    positions = {str(step["id"]): position for position, step in enumerate(earlier) if "id" in step}
    found: dict[int, Producer] = {}
    pending = list(names)
    for name in pending:
        step = _step(name)
        position = None if step is None else positions.get(step)
        if position is None or position in found:
            continue
        spec = earlier[position]
        producer = Producer(str(spec["id"]), _text(spec.get("run")), _env(spec), _text(spec.get("if")))
        found[position] = producer
        if producer.run:
            pending.extend(_reads(producer.condition, producer.env))
    return tuple(found[position] for position in sorted(found))


def _matrix(body: Mapping[str, Any]) -> dict[str, str]:
    """The matrix names a job's strategy holds at one value, each with that value as GitHub writes it: every key that
    lists exactly one value, and none when the matrix is an expression or has an include, which can add a combination
    with another value."""
    matrix = body.get("strategy", {}).get("matrix", {})
    if isinstance(matrix, str) or "include" in matrix:
        return {}
    return {f"matrix.{key}": _text(values[0]) for key, values in matrix.items() if len(values) == 1}


def _gate(
    job: str,
    step: Mapping[str, Any],
    registry: Mapping[str, Any],
    earlier: Sequence[Mapping[str, Any]],
    held: Mapping[str, str],
) -> StaticGate | None:
    if "run" not in step:
        return None
    run = str(step["run"])
    tools, modules = _commands(run)
    if not tools:
        return None
    title = str(step.get("name") or run.splitlines()[0])
    name = f"{job}/{step.get('id') or '-'.join(WORD.findall(title.lower()))}"
    entry = registry.get(name)
    paths = WHOLE_TREE if entry is None else tuple(entry["paths"])
    base_paths = WHOLE_TREE if entry is None else tuple(entry.get("base_paths", ()))
    env, condition = _env(step), _text(step.get("if"))
    own = _reads(condition, env)
    producers = _producers(earlier, own)
    reads = tuple(
        sorted(own.union(*(_reads(producer.condition, producer.env) for producer in producers if producer.run)))
    )
    matrix = tuple((read, held[read]) for read in reads if read in held)
    return StaticGate(
        name, job, title, tools, modules, run, paths, base_paths, env, condition, producers, reads, matrix
    )


def _jobs(job: str, body: Mapping[str, Any], reusable: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """The job labelled with its name, or, when it calls a reusable workflow, each job of that workflow labelled
    job/name, with its body."""
    if "uses" not in body:
        return [(job, body)]
    uses = str(body["uses"])
    workflow = reusable.get(uses.removeprefix("./.github/workflows/"))
    if workflow is None:
        raise ValueError(f"{job} uses {uses}, which was not given")
    return [(f"{job}/{name}", spec) for name, spec in workflow["jobs"].items()]


def discover(
    workflow_text: str, registry: Mapping[str, Any] | None = None, reusable: Mapping[str, Any] | None = None
) -> tuple[StaticGate, ...]:
    """Return one StaticGate per gate step of the static jobs of a workflow, in job and step order.

    reusable maps a workflow file name to its parsed workflow, as scripts.ci_wiring.load returns it. A job of a
    called workflow that calls another workflow has no steps of its own here; ci_only names it.
    """
    jobs: dict[str, Any] = yaml.safe_load(workflow_text)["jobs"]
    gates: dict[str, StaticGate] = {}
    for job in (job for job in STATIC_JOBS if job in jobs):
        for label, body in _jobs(job, jobs[job], reusable or {}):
            if "uses" in body:
                continue
            steps, held = body["steps"], _matrix(body)
            for index, step in enumerate(steps):
                gate = _gate(label, step, registry or {}, steps[:index], held)
                if gate is None:
                    continue
                if gate.name in gates:
                    raise ValueError(f"{gate.name} names two gate steps")
                gates[gate.name] = gate
    return tuple(gates.values())


def commit_ids(graded_commit: str, head: str, base: str) -> dict[str, str]:
    """Return the value of each name of COMMIT_ID_FIELDS for a grade: the graded commit when one is named, else
    head, for github.sha; head for the pull request head sha; base for the pull request base sha."""
    return dict(zip(COMMIT_ID_FIELDS, (graded_commit or head, head, base), strict=True))


def gate_commit_ids(gate: StaticGate, graded_commit: str, head: str, base: str) -> dict[str, str]:
    """Return the commit id values the gate reads, as an ordered mapping restricted to the names of
    COMMIT_ID_FIELDS it reads; empty for a gate that reads none."""
    resolved = commit_ids(graded_commit, head, base)
    return {name: resolved[name] for name in COMMIT_ID_FIELDS if name in gate.reads}


def ci_only(workflow_text: str, reusable: Mapping[str, Any] | None = None) -> tuple[CiOnly, ...]:
    """Return the jobs whose verdict stays with Actions, in job order: each job CI_ONLY names with its reason, and
    each job of a workflow a static job calls that calls another workflow, which discover grades no step of."""
    jobs: dict[str, Any] = yaml.safe_load(workflow_text)["jobs"]
    found: list[CiOnly] = []
    for job, body in jobs.items():
        if job in CI_ONLY:
            found.append(CiOnly(job, CI_ONLY[job]))
        elif job in STATIC_JOBS:
            found.extend(
                CiOnly(label, f"calls the reusable workflow {spec['uses']}")
                for label, spec in _jobs(job, body, reusable or {})
                if "uses" in spec
            )
    return tuple(found)


def main(argv: Sequence[str] | None = None) -> int:
    """Print one JSON line per static gate of the repository at --root, naming its earlier steps by id, the matrix
    values it reads and the context names it misses, with its closure digest at --grader; with --ci-only, print one
    line per job whose verdict stays with Actions, with its reason, instead."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--grader", type=Path)
    parser.add_argument("--ci-only", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root)
    workflow = (root / WORKFLOW).read_text()
    if args.ci_only:
        for entry in ci_only(workflow, ci_wiring.load(root)):
            print(json.dumps(asdict(entry)))
        return 0
    registry = json.loads((root / REGISTRY).read_text())
    for gate in discover(workflow, registry, ci_wiring.load(root)):
        record: dict[str, object] = {field: value for field, value in asdict(gate).items() if field != "run"}
        record["env"] = dict(gate.env)
        record["producers"] = [producer.id for producer in gate.producers]
        record["matrix"] = dict(gate.matrix)
        record["missing"] = list(gate.missing)
        if args.grader is not None:
            record["digest"] = grader.digest(args.grader, gate.modules)
        print(json.dumps(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
