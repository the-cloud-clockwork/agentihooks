"""Static gates discovered from the workflow tree: python -m scripts.ci_lab.static_gates [--root PATH] [--grader PATH]."""

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from scripts import ci_wiring
from scripts.ci_lab import grader

__all__ = ["STATIC_JOBS", "WHOLE_TREE", "StaticGate", "discover", "main"]

STATIC_JOBS = ("lint", "type-gate", "semgrep")
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


@dataclass(frozen=True)
class StaticGate:
    """One gate step of a static job: the repository modules it runs, the input paths that key it, and its env values
    and if condition as the workflow writes them, an empty condition when the step has none."""

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


def _module(value: str) -> str | None:
    path = CHECKOUT.sub("", value)
    return path.removesuffix(".py").replace("/", ".") if SCRIPT.fullmatch(path) else None


def _scripts(text: str, target: str) -> set[str]:
    variable = VARIABLE.fullmatch(target)
    values = [target] if variable is None else re.findall(rf'(?<![\w$]){variable.group(1)}="?([^\s";]+)', text)
    return {module for value in values if (module := _module(value)) is not None}


def _commands(run: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    parts = [part for part in COMMAND.split(run.replace("\\\n", " ")) if not SETUP_COMMAND.search(part)]
    tools = {tool for tool, pattern in TOOLS.items() if any(pattern.search(part) for part in parts)}
    modules: set[str] = set()
    for match in filter(None, map(PYTHON.match, parts)):
        flags, target = match.group(1).split(), match.group(2)
        if target not in UNGATED_MODULES:
            modules |= {target} if "-m" in flags else _scripts(run, target)
            tools.add("python")
    return tuple(sorted(tools)), tuple(sorted(modules))


def _text(value: object) -> str:
    """The text GitHub reads for a YAML scalar in a step's env or if: booleans in lower case, null as empty."""
    if value is None:
        return ""
    return str(value).lower() if isinstance(value, bool) else str(value)


def _gate(job: str, step: Mapping[str, Any], registry: Mapping[str, Any]) -> StaticGate | None:
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
    env = tuple((str(key), _text(value)) for key, value in step.get("env", {}).items())
    return StaticGate(name, job, title, tools, modules, run, paths, base_paths, env, _text(step.get("if", "")))


def _job_steps(job: str, body: Mapping[str, Any], reusable: Mapping[str, Any]) -> list[tuple[str, Any]]:
    if "uses" not in body:
        return [(job, body["steps"])]
    uses = str(body["uses"])
    workflow = reusable.get(uses.removeprefix("./.github/workflows/"))
    if workflow is None:
        raise ValueError(f"{job} uses {uses}, which was not given")
    return [(f"{job}/{name}", spec["steps"]) for name, spec in workflow["jobs"].items()]


def discover(
    workflow_text: str, registry: Mapping[str, Any] | None = None, reusable: Mapping[str, Any] | None = None
) -> tuple[StaticGate, ...]:
    """Return one StaticGate per gate step of the static jobs of a workflow, in job and step order.

    reusable maps a workflow file name to its parsed workflow, as scripts.ci_wiring.load returns it.
    """
    jobs: dict[str, Any] = yaml.safe_load(workflow_text)["jobs"]
    gates: dict[str, StaticGate] = {}
    for job in (job for job in STATIC_JOBS if job in jobs):
        for label, steps in _job_steps(job, jobs[job], reusable or {}):
            for step in steps:
                gate = _gate(label, step, registry or {})
                if gate is None:
                    continue
                if gate.name in gates:
                    raise ValueError(f"{gate.name} names two gate steps")
                gates[gate.name] = gate
    return tuple(gates.values())


def main(argv: Sequence[str] | None = None) -> int:
    """Print one JSON line per static gate of the repository at --root, with its closure digest at --grader."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--grader", type=Path)
    args = parser.parse_args(argv)
    root = Path(args.root)
    registry = json.loads((root / REGISTRY).read_text())
    for gate in discover((root / WORKFLOW).read_text(), registry, ci_wiring.load(root)):
        record: dict[str, object] = {field: value for field, value in asdict(gate).items() if field != "run"}
        record["env"] = dict(gate.env)
        if args.grader is not None:
            record["digest"] = grader.digest(args.grader, gate.modules)
        print(json.dumps(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
