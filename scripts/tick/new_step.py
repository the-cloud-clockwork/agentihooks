"""Scaffold a tick step with its budget row, feature test, scenario index row and feature map entry.

``python -m scripts.tick.new_step <name> --group <group> --lane tick|slow --interval 60|120|600 --effects <ports>
--feature <id> --module <steps module>`` writes the five artefacts, refuses a name any of them already holds and
formats the Python it wrote. A module the steps package does not hold yet is created and joins
``scripts.tick.steps.MODULES``. ``--check <name>`` prints which of the five artefacts exist and fails on a missing one.
"""

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from scripts import tick_pattern
from scripts.tick import catalogue
from scripts.tick.ports.tools import Tools

PACKAGE = "scripts.tick.steps"
STEPS = Path("scripts/tick/steps")
INIT = STEPS / "__init__.py"
BUDGETS = Path(tick_pattern.BUDGETS)
TESTS = Path("tests/tick/steps")
LANES = ("tick", "slow")
INTERVALS = {60: "CRITICAL_S", 120: "WIDE_S", 600: "SLOW_S"}
PORTS = frozenset(field.name for field in fields(Tools))
SCENARIO = "running"
IMPORTS = (
    ("Callable", "from collections.abc import Callable"),
    ("Any", "from typing import Any"),
    ("registry", "from scripts.tick import registry"),
    ("Lane", "from scripts.tick.step import Lane"),
    ("CallStep", "from scripts.tick.steps import CallStep"),
)


@dataclass(frozen=True)
class Spec:
    """One step to scaffold: name, group, lane, interval in seconds, ports, feature id, steps module and scenario."""

    name: str
    group: str
    lane: str
    interval: int
    effects: frozenset[str]
    feature: str
    module: str
    scenario: str = SCENARIO


def slug(name: str) -> str:
    """The identifier form of step ``name``."""
    return re.sub(r"\W+", "_", name.lower()).strip("_")


def test_path(name: str) -> Path:
    """The feature test file of step ``name``."""
    return TESTS / f"test_{slug(name)}.py"


def test_node(name: str) -> str:
    """The test node id of the feature test of step ``name``."""
    return f"{test_path(name).as_posix()}::test_{slug(name)}"


def _read_json(path: Path) -> Any:
    """The JSON document at ``path``."""
    return json.loads(path.read_text())


def _callee(func: ast.expr) -> str:
    """The plain name a call reaches through ``func``."""
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def registered_names(source: str) -> set[str]:
    """The literal step names ``source`` registers."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and node.args and _callee(node.func) in tick_pattern.POSITIONS):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.add(first.value)
    return found


def check(root: Path, name: str) -> dict[str, bool]:
    """Which of the five artefacts of step ``name`` the tree at ``root`` holds."""
    index = _read_json(root / catalogue.INDEX)
    features = _read_json(root / catalogue.FEATURE_MAP)["features"]
    return {
        "step": any(name in registered_names(path.read_text()) for path in sorted((root / STEPS).glob("*.py"))),
        "budget row": name in tick_pattern.floors((root / BUDGETS).read_text()),
        "test": (root / test_path(name)).is_file(),
        "index row": name in index["scenarios"] or name in index["unmapped"],
        "feature entry": any(test_node(name) in entry.get("tests", []) for entry in features),
    }


def _refuse(root: Path, spec: Spec) -> None:
    """Refuse a step whose name, ports, module or scenario the scaffold cannot write, or whose name is taken."""
    unknown = sorted(spec.effects - PORTS)
    if unknown:
        raise SystemExit(f"unknown port {', '.join(unknown)}; ports are {', '.join(sorted(PORTS))}")
    if not slug(spec.name) or not spec.group or not spec.feature:
        raise SystemExit("a step needs a name, a group and a feature id")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", spec.module) or spec.module == "budgets":
        raise SystemExit(f"{spec.module} is not a steps module name")
    if spec.scenario not in catalogue.scenarios(root):
        raise SystemExit(f"unknown scenario {spec.scenario}")
    found = [artefact for artefact, present in check(root, spec.name).items() if present]
    if found:
        raise SystemExit(f"{spec.name} already has {', '.join(found)}")


def _set_literal(items: frozenset[str]) -> str:
    """Source for a frozenset of ``items``."""
    return f"frozenset({{{', '.join(json.dumps(item) for item in sorted(items))}}})" if items else "frozenset()"


def _stub(spec: Spec) -> str:
    """Source of the step function and its registration."""
    call = slug(spec.name)
    return (
        f"def _{call}(tick: Any, get: Callable[[str], Any]) -> list[str]:\n"
        f'    """Return the actions of the {spec.name} step, given the tick module and a reader of the pass state."""\n'
        "    return []\n\n\n"
        "registry.register(\n"
        "    CallStep(\n"
        f"        {json.dumps(spec.name)},\n"
        f"        {json.dumps(spec.group)},\n"
        f"        _{call},\n"
        "        needs=frozenset(),\n"
        "        reads=frozenset(),\n"
        "        writes=frozenset(),\n"
        f"        lane=Lane({json.dumps(spec.lane)}),\n"
        f"        effects={_set_literal(spec.effects)},\n"
        f"        feature={json.dumps(spec.feature)},\n"
        "    )\n"
        ")\n"
    )


def _bound(tree: ast.Module) -> set[str]:
    """The names the top level imports of ``tree`` bind."""
    return {
        alias.asname or alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }


def _with_imports(source: str) -> str:
    """``source`` with each import the step stub needs and it lacks, after its last top level import."""
    tree = ast.parse(source)
    missing = [line + "\n" for name, line in IMPORTS if name not in _bound(tree)]
    anchors = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))] or tree.body[:1]
    at = max((node.end_lineno or 0 for node in anchors), default=0)
    lines = source.splitlines(keepends=True)
    inserted = ["\n", *missing] if missing else []
    return "".join([*lines[:at], *inserted, *lines[at:]]).rstrip("\n") + "\n"


def _step_text(root: Path, spec: Spec) -> tuple[Path, str]:
    """The path and new text for: Append the step stub to its module, creating the module when the package does not hold it."""
    path = root / STEPS / f"{spec.module}.py"
    source = path.read_text() if path.is_file() else f'"""The {spec.module} steps of the tick."""\n'
    return path, _with_imports(source) + "\n\n" + _stub(spec)


def _table_end(source: str) -> int:
    """The line of the closing brace of the budgets table."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        if node.value and any(isinstance(target, ast.Name) and target.id == "MEASURED" for target in targets):
            return node.value.end_lineno or 0
    raise SystemExit(f"{BUDGETS} holds no MEASURED table")


def _row_text(root: Path, spec: Spec) -> tuple[Path, str]:
    """The path and new text for: Insert the step's row, unmeasured at its interval, at the end of the budgets table."""
    path = root / BUDGETS
    source = path.read_text()
    lines = source.splitlines(keepends=True)
    row = f"    {json.dumps(spec.name)}: Timing(UNMEASURED_S, {INTERVALS[spec.interval]}),\n"
    lines.insert(_table_end(source) - 1, row)
    return path, "".join(lines)


def _test_source(spec: Spec) -> str:
    """Source of the step's feature test."""
    expected = ", ".join(json.dumps(value) for value in (spec.group, spec.lane, spec.feature))
    return (
        f'"""The {spec.name} tick step registers with its group, lane, feature and ports."""\n\n'
        "from importlib import import_module\n\n"
        "import pytest\n\n"
        "from scripts.tick import registry\n\n\n"
        f"@pytest.mark.feature({json.dumps(spec.feature)})\n"
        f"def test_{slug(spec.name)}() -> None:\n"
        f'    """The {spec.name} step registers in group {spec.group} on the {spec.lane} lane with its feature and ports."""\n'
        f"    import_module({json.dumps(f'{PACKAGE}.{spec.module}')})\n"
        f"    found = {{step.name: step for step in registry.steps()}}[{json.dumps(spec.name)}]\n"
        "    assert (found.group, found.lane, found.feature, found.effects) == (\n"
        f"        {expected}, {_set_literal(spec.effects)}\n"
        "    )\n"
    )


def _test_text(root: Path, spec: Spec) -> tuple[Path, str]:
    """The path and new text for: Write the step's feature test."""
    path = root / test_path(spec.name)
    return path, _test_source(spec)


def _index_text(root: Path, spec: Spec) -> tuple[Path, str]:
    """The path and new text for: List the step under its scenario in the scenario index."""
    path = root / catalogue.INDEX
    index = _read_json(path)
    index["scenarios"] = dict(sorted({**index["scenarios"], spec.name: [spec.scenario]}.items()))
    return path, json.dumps(index, indent=1) + "\n"


def _feature_text(root: Path, spec: Spec) -> tuple[Path, str]:
    """The path and new text for: Add the step's test to its feature map entry, creating an inventory entry for a new feature id."""
    path = root / catalogue.FEATURE_MAP
    data = _read_json(path)
    entry = next((entry for entry in data["features"] if entry["id"] == spec.feature), None)
    if entry is None:
        data["features"].append(
            {
                "id": spec.feature,
                "origin": "inventory",
                "behaviour": f"The tick step {spec.name} runs in group {spec.group} on the {spec.lane} lane "
                f"every {spec.interval} seconds.",
                "seam": f"{PACKAGE}.{spec.module}._{slug(spec.name)} registered as tick step {spec.name}",
                "tests": [test_node(spec.name)],
            }
        )
    else:
        entry["tests"] = [*entry.get("tests", []), test_node(spec.name)]
    return path, json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _join_modules(root: Path, module: str) -> tuple[Path, str]:
    """The path and new text for: Append ``module`` to the ``MODULES`` tuple of the steps package."""
    path = root / INIT
    source = path.read_text()
    value = next(
        node.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MODULES" for t in node.targets)
    )
    names = [*ast.literal_eval(value), f"{PACKAGE}.{module}"]
    lines = source.splitlines(keepends=True)
    first, last = value.lineno - 1, (value.end_lineno or value.lineno) - 1
    head, tail = lines[first][: value.col_offset], lines[last][value.end_col_offset or 0 :]
    lines[first : last + 1] = [head + "(" + "".join(f"{json.dumps(name)}, " for name in names) + ")" + tail]
    return path, "".join(lines)


def _format(root: Path, paths: list[Path]) -> None:
    """Sort the imports of and format the Python files at ``paths``."""
    ruff = [sys.executable, "-m", "ruff"]
    targets = [str(path) for path in paths]
    subprocess.run([*ruff, "check", "--select", "I", "--fix", "--quiet", *targets], cwd=root, check=True)
    subprocess.run([*ruff, "format", "--quiet", *targets], cwd=root, check=True)


def scaffold(root: Path, spec: Spec) -> list[Path]:
    """Write the five artefacts of ``spec`` in the tree at ``root`` and return the files written."""
    _refuse(root, spec)
    new = not (root / STEPS / f"{spec.module}.py").is_file()
    texts = [
        _step_text(root, spec),
        _row_text(root, spec),
        _test_text(root, spec),
        _index_text(root, spec),
        _feature_text(root, spec),
    ]
    if new:
        texts.append(_join_modules(root, spec.module))
    for path, text in texts:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    written = [path for path, _ in texts]
    _format(root, [path for path in written if path.suffix == ".py"])
    return written


def _parser() -> argparse.ArgumentParser:
    """The command line of the scaffold."""
    parser = argparse.ArgumentParser(prog="python -m scripts.tick.new_step")
    parser.add_argument("name", nargs="?")
    parser.add_argument("--check", metavar="NAME")
    parser.add_argument("--group")
    parser.add_argument("--lane", choices=LANES, default="tick")
    parser.add_argument("--interval", type=int, choices=sorted(INTERVALS), default=60)
    parser.add_argument("--effects", default="")
    parser.add_argument("--feature")
    parser.add_argument("--module")
    parser.add_argument("--scenario", default=SCENARIO)
    parser.add_argument("--root", type=Path, default=Path("."))
    return parser


def main(argv: list[str] | None = None) -> int:
    """Scaffold a step, or report which artefacts of a step exist with ``--check``."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.check:
        found = check(args.root, args.check)
        print(json.dumps(found, indent=1))
        return 0 if all(found.values()) else 1
    if not (args.name and args.group and args.feature and args.module):
        parser.error("a step needs a name, --group, --feature and --module")
    effects = frozenset(port.strip() for port in args.effects.split(",") if port.strip())
    spec = Spec(args.name, args.group, args.lane, args.interval, effects, args.feature, args.module, args.scenario)
    for path in scaffold(args.root, spec):
        print(path.relative_to(args.root).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
